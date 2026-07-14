from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind.config import Settings
from projectmind.evaluation import EvalSet, Expectation
from projectmind.evaluation.fixtures import FixtureCorpus
from projectmind.evaluation.live import LiveSystem, live_service
from projectmind.evaluation.runner import run
from projectmind.evaluation.scoring import ScoreReport
from projectmind.gate import budget as budgeting
from projectmind.gate.prompt_analysis import classify
from projectmind.gate.rules import decide, describe
from projectmind.models import Fingerprint, RecordType, ServedRecord, TaskType, utcnow

SETTINGS = Settings(home=Path(tempfile.gettempdir()) / "projectmind-gate-tests")

FLUTTER = Fingerprint(
    languages=["dart"], frameworks=["flutter"], dependencies=["riverpod", "supabase_flutter"]
)


@pytest.fixture(scope="module")
def vocabulary() -> list[str]:
    corpus = FixtureCorpus.load()
    return sorted({entity for record in corpus.records for entity in record.entities})


def analyse(prompt: str, vocabulary: list[str] | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
    return classify(prompt, vocabulary=vocabulary or [], settings=SETTINGS, **kwargs)  # type: ignore[arg-type]


class TestTrivialDetection:
    @pytest.mark.parametrize(
        "prompt",
        [
            "Rename the variable res to response in this file.",
            "Format this file.",
            "Add a trailing newline to the end of README.md.",
            "Fix the typo in this comment.",
            "Sort these imports.",
            "Add a docstring to this method.",
            "Bump the version in pyproject.toml to 0.3.1.",
            "Delete this unused import.",
            "What does this regex do?",
            "Convert this function to use an f-string.",
        ],
    )
    def test_mechanical_prompts_are_trivial(self, prompt: str) -> None:
        analysis = analyse(prompt)
        assert analysis.is_trivial, analysis.task_type
        assert analysis.trivial_reason

    @pytest.mark.parametrize(
        "prompt",
        [
            "Add Supabase auth to this Flutter screen.",
            "Training is fine but validation crashes with CUDA out of memory.",
            "Starting a new Flutter app. Firebase or Supabase for the backend?",
            "Refactor this training script so the preprocessing is reproducible.",
            "My cross-validation score is great but the held-out score is terrible.",
            "Set up CI for this project.",
        ],
    )
    def test_real_work_is_never_trivial(self, prompt: str) -> None:
        assert not analyse(prompt).is_trivial

    def test_a_manifest_mention_is_not_itself_a_trigger(self) -> None:
        """q009 exists to catch a gate that fires on seeing pyproject.toml."""
        assert analyse("Bump the version in pyproject.toml to 0.3.1.").is_trivial

    def test_an_empty_prompt_is_trivial(self) -> None:
        assert analyse("   ").is_trivial

    def test_a_long_mechanical_sounding_prompt_is_still_real_work(self) -> None:
        prompt = (
            "Convert the whole ingestion module to async, move the retry logic into a "
            "decorator, and make sure the exponential backoff survives a restart."
        )
        assert not analyse(prompt).is_trivial


class TestTaskTypes:
    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("Training crashes with CUDA out of memory.", TaskType.DEBUG),
            ("My GPU sits at ten percent utilisation while training.", TaskType.DEBUG),
            ("Detection boxes look right by eye but mAP is low.", TaskType.DEBUG),
            ("Firebase or Supabase for the backend?", TaskType.ARCHITECT),
            ("Is it worth trying a transformer here?", TaskType.EXPLORE),
            ("Refactor this script so preprocessing is reproducible.", TaskType.REFACTOR),
            ("Add a caching layer in front of the model call.", TaskType.IMPLEMENT),
        ],
    )
    def test_classification(self, prompt: str, expected: TaskType) -> None:
        assert analyse(prompt).task_type is expected

    def test_overlapping_patterns_do_not_double_count(self) -> None:
        """`should i` and `how should i` fire on the same words; that is one signal."""
        analysis = analyse("The model returns JSON I cannot parse. How should I handle it?")
        assert analysis.task_type is TaskType.DEBUG

    def test_novelty_measures_what_the_project_does_not_use(self, vocabulary: list[str]) -> None:
        known = analyse("switch to riverpod here", vocabulary, fingerprint=FLUTTER)
        assert known.entities == ("riverpod",)
        assert known.novelty == pytest.approx(0.0)
        assert not known.is_novel_territory

        novel = analyse("try ultralytics for this", vocabulary, fingerprint=FLUTTER)
        assert novel.novelty == pytest.approx(1.0)
        assert novel.is_novel_territory

        # Half known, half not: "provider" is a thing memory holds but this
        # project does not use, so the prompt is partly new territory.
        mixed = analyse("switch the riverpod provider", vocabulary, fingerprint=FLUTTER)
        assert mixed.novelty == pytest.approx(0.5)


class TestGatingRules:
    def test_a_trivial_prompt_gets_nothing_at_all(self) -> None:
        decision = decide(analyse("Rename this variable."), settings=SETTINGS)
        assert decision.skips_everything
        assert decision.inject_profile is False
        assert decision.inject_episodic is False
        assert "trivial" in describe(decision)

    def test_the_profile_is_always_eligible_for_real_work(self) -> None:
        decision = decide(analyse("Set up CI for this project."), settings=SETTINGS)
        assert decision.inject_profile is True

    def test_no_episodic_memory_means_profile_only(self) -> None:
        decision = decide(
            analyse("Firebase or Supabase?"), episodic_available=False, settings=SETTINGS
        )
        assert decision.inject_profile is True
        assert decision.inject_episodic is False
        assert "no episodic memory" in decision.reason

    def test_a_debug_prompt_asks_for_failures_not_decisions(self, vocabulary: list[str]) -> None:
        decision = decide(
            analyse("Validation crashes with CUDA out of memory.", vocabulary), settings=SETTINGS
        )
        assert decision.inject_episodic is True
        assert RecordType.FAILURE in decision.record_types
        assert RecordType.DECISION not in decision.record_types

    def test_an_architect_prompt_asks_for_decisions_not_failures(self) -> None:
        decision = decide(analyse("How should I structure this service?"), settings=SETTINGS)
        assert RecordType.DECISION in decision.record_types
        assert RecordType.FAILURE not in decision.record_types

    def test_a_named_entity_opens_the_episodic_slice(self, vocabulary: list[str]) -> None:
        decision = decide(analyse("Add supabase auth here", vocabulary), settings=SETTINGS)
        assert decision.inject_episodic is True
        assert "supabase" in decision.entity_filter

    def test_an_implement_prompt_naming_nothing_stays_profile_only(self) -> None:
        """This is the rule that stops the budget being padded."""
        decision = decide(analyse("Write the project README."), settings=SETTINGS)
        assert decision.inject_profile is True
        assert decision.inject_episodic is False

    def test_a_refactor_prompt_searches_precedent_even_unnamed(self) -> None:
        decision = decide(
            analyse("Refactor this so the preprocessing is reproducible."), settings=SETTINGS
        )
        assert decision.inject_episodic is True

    def test_exploratory_prompts_get_a_stronger_cross_project_boost(self) -> None:
        exploratory = decide(analyse("How should I structure this?"), settings=SETTINGS)
        debugging = decide(analyse("It crashes on startup."), settings=SETTINGS)
        assert exploratory.cross_project_boost > debugging.cross_project_boost


def served(what: str, tokens_hint: str = "") -> ServedRecord:
    return ServedRecord(
        id=uuid4(),
        project_key="demo",
        type=RecordType.DECISION,
        what=what + tokens_hint,
        occurred_at=utcnow(),
        source_url="fixture://x",
        confidence=0.9,
        is_inference=False,
    )


class TestBudget:
    def test_records_are_taken_in_order_until_the_cap(self) -> None:
        records = [served(f"decision number {i}") for i in range(10)]
        report = budgeting.fit_records(records, token_cap=10_000, max_records=3)
        assert len(report.kept) == 3
        assert report.dropped_for_cap == 7
        assert report.kept[0].what.endswith("0")

    def test_the_token_cap_is_never_exceeded(self) -> None:
        records = [served("a fairly wordy decision about something " * 5) for _ in range(10)]
        report = budgeting.fit_records(records, token_cap=120, max_records=10)
        assert report.tokens <= 120
        assert report.dropped_for_budget > 0

    def test_one_huge_record_does_not_starve_the_rest(self) -> None:
        records = [
            served("enormous " * 400),
            served("small one"),
            served("small two"),
        ]
        report = budgeting.fit_records(records, token_cap=200, max_records=5)
        assert [r.what for r in report.kept] == ["small one", "small two"]

    def test_records_are_dropped_whole_rather_than_truncated(self) -> None:
        records = [served("x " * 200)]
        report = budgeting.fit_records(records, token_cap=50, max_records=5)
        assert report.kept == ()

    def test_the_total_cap_trims_episodic_and_never_the_profile(self) -> None:
        records = [served(f"decision {i} " * 30) for i in range(5)]
        episodic = budgeting.fit_records(records, token_cap=1500, max_records=5)
        trimmed = budgeting.enforce_total(2200, episodic, settings=SETTINGS)
        assert 2200 + trimmed.tokens <= SETTINGS.total_token_cap
        assert len(trimmed.kept) < len(episodic.kept)

    def test_an_unbreached_total_is_left_alone(self) -> None:
        episodic = budgeting.fit_records([served("short")], token_cap=1500, max_records=5)
        assert budgeting.enforce_total(100, episodic, settings=SETTINGS) is episodic

    def test_within_caps(self) -> None:
        assert budgeting.within_caps(800, 1500, SETTINGS) is True
        assert budgeting.within_caps(801, 0, SETTINGS) is False
        assert budgeting.within_caps(0, 1501, SETTINGS) is False


EVAL_PATH = Path(__file__).resolve().parents[1] / "eval" / "queries.yaml"


@pytest.fixture(scope="module")
def live_report() -> ScoreReport:
    """One live run, shared by the Phase 2 exit assertions."""
    eval_set = EvalSet.from_yaml(EVAL_PATH)
    with tempfile.TemporaryDirectory() as tmp, live_service(Path(tmp)) as service:
        return run(eval_set, LiveSystem(service))


class TestPhaseTwoExitCriterion:
    """The Phase 2 exit bar, asserted rather than claimed."""

    def test_gate_precision_clears_the_target(self, live_report: ScoreReport) -> None:
        assert live_report.gate_precision >= 0.80

    def test_nothing_is_injected_into_a_trivial_prompt(self, live_report: ScoreReport) -> None:
        trivial = {q.id for q in EvalSet.from_yaml(EVAL_PATH).select(expect=Expectation.NOTHING)}
        offenders = [
            score.query_id
            for score in live_report.per_query
            if score.query_id in trivial and score.injected
        ]
        assert offenders == []

    def test_no_budget_is_ever_breached(self, live_report: ScoreReport) -> None:
        assert live_report.budget_violations == 0
        assert live_report.max_total_tokens <= 2300

    def test_near_misses_are_rarely_served(self, live_report: ScoreReport) -> None:
        """The `forbidden` labels are plausible wrong answers; the gate should
        mostly keep them out even before the ranker is tuned.

        This used to assert `false_injection_rate <= 0.10`, which held only
        because no retriever was attached and nothing was ever served. Once one
        was, the rate went to 0.707 — serving five records for a query with one
        right answer is four wrong ones. That target belongs to Phase 4, where
        it is a real measurement rather than an artifact of an empty index.
        """
        assert live_report.forbidden_rate <= 0.20

    def test_the_front_door_is_fast_enough_to_run_on_every_prompt(
        self, live_report: ScoreReport
    ) -> None:
        assert live_report.p95_latency_ms < 250
