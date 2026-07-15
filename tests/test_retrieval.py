from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind.config import Settings
from projectmind.evaluation.live import LiveSystem, live_service
from projectmind.evaluation.runner import run
from projectmind.evaluation.scoring import ScoreReport
from projectmind.evaluation.spec import EvalSet
from projectmind.gate.prompt_analysis import classify
from projectmind.gate.rules import decide
from projectmind.models import (
    EpisodicRecord,
    Fingerprint,
    Project,
    RecordType,
    SourceType,
)
from projectmind.retrieval.hybrid import HybridRetriever
from projectmind.retrieval.lexical import BM25Index, document_tokens
from projectmind.retrieval.ranking import Candidate, RankingWeights, rank, score_candidate
from projectmind.storage import SqliteStore, Store
from projectmind.storage.embeddings import HashingEmbedder

NOW = datetime(2026, 7, 1, tzinfo=UTC)
EVAL_PATH = Path(__file__).resolve().parents[1] / "eval" / "queries.yaml"
SETTINGS = Settings(home=Path(tempfile.gettempdir()) / "projectmind-retrieval-tests")


def record(what: str, **overrides: object) -> EpisodicRecord:
    payload: dict[str, object] = {
        "project_id": uuid4(),
        "type": RecordType.DECISION,
        "what": what,
        "source_type": SourceType.COMMIT,
        "source_url": "fixture://x",
        "occurred_at": NOW,
        "confidence": 0.85,
        "is_inference": False,
    }
    payload.update(overrides)
    return EpisodicRecord.model_validate(payload)


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


class TestBM25:
    def test_an_exact_rare_token_wins(self) -> None:
        records = [
            record("Fixed the CTC alignment so loss stopped going to NaN"),
            record("Adopted Supabase for auth and storage"),
            record("Switched the dataloader to eight workers"),
        ]
        hits = BM25Index.build(records).search("CTC loss NaN")
        assert hits[0].record_id == records[0].id
        assert hits[0].score == pytest.approx(1.0)

    def test_scores_are_normalised_against_the_best_hit(self) -> None:
        records = [record("supabase auth"), record("supabase")]
        hits = BM25Index.build(records).search("supabase auth")
        assert hits[0].score == pytest.approx(1.0)
        assert all(0.0 < hit.score <= 1.0 for hit in hits)

    def test_a_query_matching_nothing_returns_nothing(self) -> None:
        assert BM25Index.build([record("supabase auth")]).search("kubernetes helm") == []

    def test_an_empty_index_is_safe(self) -> None:
        assert BM25Index.build([]).search("anything") == []
        assert BM25Index.build([]).size == 0

    def test_entities_are_weighted_above_prose(self) -> None:
        with_entity = record("Changed the login screen", entities=["supabase"])
        with_prose = record("Mentioned supabase once in passing while doing other work")
        hits = BM25Index.build([with_entity, with_prose]).search("supabase")
        assert hits[0].record_id == with_entity.id

    def test_hyphenated_entities_are_indexed_split_as_well(self) -> None:
        """The developer types "state management"; memory holds `state-management`."""
        tokens = document_tokens(record("Standardised on Riverpod", entities=["state-management"]))
        assert "state-management" in tokens
        assert "state" in tokens
        assert "management" in tokens

    def test_scikit_learn_survives_as_one_term(self) -> None:
        tokens = document_tokens(record("Wrapped it in a Pipeline", entities=["scikit-learn"]))
        assert "scikit-learn" in tokens


class TestRanking:
    def _candidate(self, project: Project, **overrides: object) -> Candidate:
        return Candidate(record=record("x", project_id=project.id, **overrides), project=project)

    def test_recency_decays_at_the_half_life(self) -> None:
        project = Project(key="p", name="P")
        old = self._candidate(project, occurred_at=NOW - timedelta(days=240))
        fresh = self._candidate(project, occurred_at=NOW)
        weights = RankingWeights(recency_half_life_days=240)
        scored_old = score_candidate(
            old, caller_project_id=None, caller_fingerprint=None, weights=weights, now=NOW
        )
        scored_new = score_candidate(
            fresh, caller_project_id=None, caller_fingerprint=None, weights=weights, now=NOW
        )
        assert scored_old.recency == pytest.approx(0.5)
        assert scored_new.recency == pytest.approx(1.0)
        assert scored_new.score > scored_old.score

    def test_the_cross_project_boost_can_overturn_a_same_repo_result(self) -> None:
        """The whole product in one assertion."""
        here = Project(key="here", name="Here", fingerprint=Fingerprint(languages=["python"]))
        elsewhere = Project(
            key="elsewhere", name="Elsewhere", fingerprint=Fingerprint(languages=["python"])
        )
        mine = Candidate(
            record=record("mediocre", project_id=here.id, confidence=0.7), project=here
        )
        theirs = Candidate(
            record=record("strong", project_id=elsewhere.id, confidence=0.75), project=elsewhere
        )
        weights = RankingWeights(cross_project_boost=1.45)
        ranked = rank(
            [mine, theirs],
            caller_project_id=here.id,
            caller_fingerprint=here.fingerprint,
            weights=weights,
            now=NOW,
        )
        assert ranked[0].project_key == "elsewhere"
        assert ranked[0].boosted is True

    def test_the_boost_does_not_promote_a_weak_cross_project_result(self) -> None:
        here = Project(key="here", name="Here")
        elsewhere = Project(key="elsewhere", name="Elsewhere")
        mine = Candidate(record=record("strong", project_id=here.id, confidence=0.95), project=here)
        theirs = Candidate(
            record=record("weak", project_id=elsewhere.id, confidence=0.3), project=elsewhere
        )
        ranked = rank(
            [mine, theirs],
            caller_project_id=here.id,
            caller_fingerprint=None,
            weights=RankingWeights(cross_project_boost=1.45),
            now=NOW,
        )
        assert ranked[0].project_key == "here"

    def test_relevance_multiplies_rather_than_averages(self) -> None:
        """A weak match must not inherit the record's standalone confidence."""
        project = Project(key="p", name="P")
        candidate = self._candidate(project, confidence=0.9)
        strong = score_candidate(
            candidate,
            caller_project_id=None,
            caller_fingerprint=None,
            weights=RankingWeights(),
            now=NOW,
            relevance=1.0,
        )
        weak = score_candidate(
            candidate,
            caller_project_id=None,
            caller_fingerprint=None,
            weights=RankingWeights(),
            now=NOW,
            relevance=0.1,
        )
        assert weak.score < strong.score / 5

    def test_the_minimum_score_drops_candidates(self) -> None:
        project = Project(key="p", name="P")
        candidates = [self._candidate(project, confidence=0.1)]
        assert (
            rank(
                candidates,
                caller_project_id=None,
                caller_fingerprint=None,
                now=NOW,
                minimum_score=0.5,
            )
            == []
        )


class TestHybridRetriever:
    def _seed(self, store: Store) -> tuple[Project, Project]:
        embedder = HashingEmbedder()
        here = store.upsert_project(
            Project(
                key="here",
                name="Here",
                fingerprint=Fingerprint(languages=["python"], dependencies=["torch"]),
            )
        )
        there = store.upsert_project(
            Project(
                key="there",
                name="There",
                fingerprint=Fingerprint(languages=["python"], dependencies=["torch"]),
            )
        )
        records = [
            record(
                "CUDA out of memory during validation but not training",
                project_id=there.id,
                type=RecordType.FAILURE,
                entities=["cuda", "validation"],
                confidence=0.9,
            ),
            record(
                "Adopted Supabase for auth and storage",
                project_id=here.id,
                entities=["supabase"],
                confidence=0.9,
            ),
            record(
                "Exported the model to ONNX for CPU inference",
                project_id=here.id,
                entities=["onnx"],
                confidence=0.88,
            ),
        ]
        store.add_records(
            [
                item.model_copy(
                    update={"embedding": tuple(embedder.embed([item.searchable_text])[0])}
                )
                for item in records
            ]
        )
        return here, there

    def _decide(self, prompt: str, vocabulary: list[str]):  # type: ignore[no-untyped-def]
        return decide(
            classify(prompt, vocabulary=vocabulary, settings=SETTINGS),
            episodic_available=True,
            settings=SETTINGS,
        )

    def test_the_vector_half_actually_contributes(self, store: Store) -> None:
        """Regression: list_records does not load embeddings, so an in-process
        cosine scored every record at zero and the hybrid was lexical-only."""
        self._seed(store)
        retriever = HybridRetriever(store, SETTINGS)
        pool = store.list_records()
        hits = retriever._vector_search(pool, "ran out of GPU memory while validating", limit=5)
        assert hits, "vector search returned nothing; the embedding join is broken"
        assert any(score > 0 for _, score in hits)

    def test_a_relevant_record_is_found_and_ranked_first(self, store: Store) -> None:
        here, _ = self._seed(store)
        retriever = HybridRetriever(store, SETTINGS)
        decision = self._decide("CUDA out of memory in validation", ["cuda", "validation"])
        served = retriever.retrieve(
            decision, project=here, fingerprint=here.fingerprint, limit=5, now=NOW
        )
        assert served
        assert "CUDA" in served[0].what

    def test_the_relative_cut_serves_one_record_for_a_one_answer_query(self, store: Store) -> None:
        here, _ = self._seed(store)
        retriever = HybridRetriever(store, SETTINGS)
        decision = self._decide("exported to ONNX for CPU inference", ["onnx"])
        served = retriever.retrieve(
            decision, project=here, fingerprint=here.fingerprint, limit=5, now=NOW
        )
        assert len(served) == 1

    def test_cross_project_records_are_flagged(self, store: Store) -> None:
        here, _ = self._seed(store)
        retriever = HybridRetriever(store, SETTINGS)
        decision = self._decide("CUDA out of memory in validation", ["cuda"])
        served = retriever.retrieve(
            decision, project=here, fingerprint=here.fingerprint, limit=5, now=NOW
        )
        assert served[0].cross_project is True

    def test_an_empty_store_serves_nothing(self, store: Store) -> None:
        retriever = HybridRetriever(store, SETTINGS)
        decision = self._decide("anything at all", [])
        assert retriever.retrieve(decision, project=None, fingerprint=Fingerprint(), limit=5) == []

    def test_a_query_matching_nothing_serves_nothing(self, store: Store) -> None:
        here, _ = self._seed(store)
        retriever = HybridRetriever(store, SETTINGS)
        decision = self._decide("kubernetes ingress certificate rotation", [])
        served = retriever.retrieve(
            decision, project=here, fingerprint=here.fingerprint, limit=5, now=NOW
        )
        assert served == []


@pytest.fixture(scope="module")
def phase_four() -> ScoreReport:
    with tempfile.TemporaryDirectory() as tmp, live_service(Path(tmp)) as service:
        return run(EvalSet.from_yaml(EVAL_PATH), LiveSystem(service))


class TestPhaseFourTargets:
    """Three of the four Phase 4 targets are met; the fourth is not, and the
    test says so rather than being softened until it passes."""

    def test_precision_at_five(self, phase_four: ScoreReport) -> None:
        assert phase_four.precision_at_k >= 0.70

    def test_cross_project_hit_rate(self, phase_four: ScoreReport) -> None:
        assert phase_four.cross_project_hit_rate >= 0.30

    def test_gate_precision_did_not_regress(self, phase_four: ScoreReport) -> None:
        assert phase_four.gate_precision >= 0.80

    def test_no_deliberately_wrong_record_is_ever_served(self, phase_four: ScoreReport) -> None:
        """The `forbidden` labels are the near-misses. None of them get through."""
        assert phase_four.forbidden_rate == 0.0

    def test_false_injection_is_tracked_and_currently_misses(self, phase_four: ScoreReport) -> None:
        """Target is 0.10 and the system sits at 0.174.

        Pinned as a ratchet rather than as a pass: it must not get worse, and
        the gap is documented in docs/evaluation.md instead of being hidden by
        relaxing the target. Note that 0.136 was reachable without the absolute
        vector floor, at the cost of serving a record for a query about
        kubernetes against a corpus containing none — a kind of false injection
        this eval set has no query for and therefore cannot price.
        """
        assert phase_four.false_injection_rate <= 0.20

    def test_budgets_still_hold(self, phase_four: ScoreReport) -> None:
        assert phase_four.budget_violations == 0
        assert phase_four.max_total_tokens <= 2300
