from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from projectmind.config import Settings
from projectmind.models import (
    Category,
    Fingerprint,
    ProfileStatement,
    StatementStatus,
)
from projectmind.profile.repository import (
    RELEVANCE_DIRECT,
    RELEVANCE_GENERAL,
    RELEVANCE_UNRELATED,
    ProfileRepository,
)
from projectmind.profile.seed import SeedFile, seed_profile
from projectmind.storage import SqliteStore, Store

NOW = datetime(2026, 7, 1, tzinfo=UTC)

FLUTTER = Fingerprint(
    languages=["dart"],
    frameworks=["flutter"],
    dependencies=["riverpod", "supabase_flutter", "go_router", "freezed"],
)
ML = Fingerprint(
    languages=["python"],
    frameworks=["pytorch"],
    dependencies=["torch", "ultralytics", "scikit-learn", "optuna"],
)


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


@pytest.fixture
def repo(store: Store) -> ProfileRepository:
    return ProfileRepository(store, Settings(home=".projectmind-test"))


def active(
    text: str, category: Category = Category.TOOL_PREFERENCE, **kw: object
) -> ProfileStatement:
    payload: dict[str, object] = {
        "statement": text,
        "category": category,
        "confidence": 0.9,
        "status": StatementStatus.ACTIVE,
        "last_confirmed_at": NOW,
        "created_at": NOW,
    }
    payload.update(kw)
    return ProfileStatement.model_validate(payload)


class TestSeeding:
    def test_the_seed_file_holds_fifty_statements_across_all_four_categories(self) -> None:
        seed = SeedFile.load()
        assert len(seed) == 50
        counts = Counter(s.category for s in seed.statements)
        assert set(counts) == set(Category)
        assert counts[Category.TOOL_PREFERENCE] >= 15

    def test_seeded_statements_are_proposed_and_therefore_not_served(
        self, repo: ProfileRepository
    ) -> None:
        seed_profile(repo, now=NOW)
        assert len(repo.all()) == 50
        assert repo.active() == []
        assert repo.select(fingerprint=FLUTTER, now=NOW).is_empty

    def test_seeding_twice_does_not_duplicate(self, repo: ProfileRepository) -> None:
        first = seed_profile(repo, now=NOW)
        second = seed_profile(repo, now=NOW)
        assert first.loaded == 50
        assert second.loaded == 0
        assert second.skipped_duplicates == 50
        assert len(repo.all()) == 50

    def test_activate_all_is_opt_in(self, repo: ProfileRepository) -> None:
        result = seed_profile(repo, activate=True, now=NOW)
        assert result.activated == 50
        assert len(repo.active()) == 50

    def test_every_seeded_statement_carries_provenance(self, repo: ProfileRepository) -> None:
        seed_profile(repo, now=NOW)
        assert all(s.evidence_refs for s in repo.all())

    def test_ttls_come_from_the_category(self, repo: ProfileRepository) -> None:
        seed_profile(repo, now=NOW)
        by_category = {s.category: s.ttl_days for s in repo.all()}
        assert by_category[Category.TOOL_PREFERENCE] == 90
        assert by_category[Category.ABANDONED] == 365


class TestSelection:
    def test_nothing_active_means_an_empty_slice(self, repo: ProfileRepository) -> None:
        assert repo.select(fingerprint=FLUTTER, now=NOW).is_empty

    def test_fingerprint_match_outranks_an_unrelated_statement(
        self, repo: ProfileRepository
    ) -> None:
        repo.add(active("Uses Riverpod for Flutter state management", entities=["riverpod"]))
        repo.add(active("Uses Ultralytics YOLO for detection baselines", entities=["ultralytics"]))
        chosen = repo.select(fingerprint=FLUTTER, now=NOW)
        assert chosen.statements[0].statement.entities == ("riverpod",)
        assert chosen.statements[0].relevance == RELEVANCE_DIRECT
        assert chosen.statements[1].relevance == RELEVANCE_UNRELATED

    def test_a_prompt_entity_promotes_an_otherwise_unrelated_statement(
        self, repo: ProfileRepository
    ) -> None:
        repo.add(
            active("Abandoned SMOTE for class imbalance", Category.ABANDONED, entities=["smote"])
        )
        repo.add(active("Uses Riverpod for Flutter state", entities=["riverpod"]))
        without = repo.select(fingerprint=FLUTTER, now=NOW)
        assert without.statements[0].statement.entities == ("riverpod",)
        with_prompt = repo.select(fingerprint=FLUTTER, entities=["smote"], now=NOW)
        assert with_prompt.statements[0].statement.entities == ("smote",)
        assert with_prompt.statements[0].reason == "named in the prompt"

    def test_work_style_applies_regardless_of_stack(self, repo: ProfileRepository) -> None:
        repo.add(active("Writes the evaluation before the pipeline", Category.WORK_STYLE))
        for fingerprint in (FLUTTER, ML):
            chosen = repo.select(fingerprint=fingerprint, now=NOW)
            assert chosen.statements[0].relevance == RELEVANCE_GENERAL

    def test_constraints_are_stack_agnostic_even_when_they_name_entities(
        self, repo: ProfileRepository
    ) -> None:
        repo.add(active("The local GPU has 6 GB of memory", Category.CONSTRAINT, entities=["gpu"]))
        chosen = repo.select(fingerprint=FLUTTER, now=NOW)
        assert chosen.statements[0].relevance == RELEVANCE_GENERAL

    def test_the_statement_cap_is_enforced_separately_from_the_token_budget(
        self, repo: ProfileRepository
    ) -> None:
        for index in range(30):
            repo.add(active(f"Prefers tool number {index} for this kind of task"))
        chosen = repo.select(fingerprint=FLUTTER, now=NOW, max_statements=5)
        assert len(chosen.statements) == 5
        assert chosen.dropped_for_cap == 25
        assert chosen.dropped_for_budget == 0

    def test_the_token_budget_is_never_exceeded(self, repo: ProfileRepository) -> None:
        for index in range(30):
            repo.add(active(f"Prefers the tool called number {index} for this kind of task"))
        chosen = repo.select(fingerprint=FLUTTER, now=NOW, token_budget=60, max_statements=50)
        assert chosen.tokens <= 60
        assert chosen.dropped_for_budget > 0

    def test_a_real_seeded_profile_fits_comfortably_under_eight_hundred_tokens(
        self, repo: ProfileRepository
    ) -> None:
        """The Phase 1 exit criterion, asserted rather than assumed."""
        seed_profile(repo, activate=True, now=NOW)
        for fingerprint in (FLUTTER, ML, Fingerprint()):
            chosen = repo.select(fingerprint=fingerprint, now=NOW)
            assert chosen.tokens <= 800, fingerprint
            assert len(chosen.statements) <= 15

    def test_decayed_statements_rank_below_fresh_ones(self, repo: ProfileRepository) -> None:
        repo.add(active("Uses Riverpod for Flutter state", entities=["riverpod"]))
        repo.add(
            active(
                "Uses go_router for Flutter navigation",
                entities=["go_router"],
                last_confirmed_at=NOW - timedelta(days=200),
            )
        )
        chosen = repo.select(fingerprint=FLUTTER, now=NOW)
        assert chosen.statements[0].statement.entities == ("riverpod",)
        assert chosen.statements[1].score < chosen.statements[0].score

    def test_fully_decayed_statements_are_not_served_at_all(self, repo: ProfileRepository) -> None:
        repo.add(
            active(
                "Uses Riverpod for Flutter state",
                entities=["riverpod"],
                last_confirmed_at=NOW - timedelta(days=400),
            )
        )
        chosen = repo.select(fingerprint=FLUTTER, now=NOW)
        assert chosen.is_empty
        assert chosen.dropped_for_relevance == 1

    def test_selection_without_a_fingerprint_still_serves_general_statements(
        self, repo: ProfileRepository
    ) -> None:
        repo.add(active("Writes the evaluation before the pipeline", Category.WORK_STYLE))
        repo.add(active("Uses Riverpod for Flutter state", entities=["riverpod"]))
        chosen = repo.select(now=NOW)
        assert chosen.statements[0].relevance == RELEVANCE_GENERAL

    def test_served_statements_carry_their_scope(self, repo: ProfileRepository) -> None:
        repo.add(
            active(
                "Prefers Firebase for backend-as-a-service",
                entities=["firebase"],
                scope={"when": "throwaway prototypes"},
            )
        )
        served = repo.select(now=NOW).served()
        assert served[0].scope == "when: throwaway prototypes"


class TestMaintenance:
    def test_sweep_marks_only_the_statements_three_ttls_past_due(
        self, repo: ProfileRepository
    ) -> None:
        repo.add(active("Fresh preference that was confirmed recently"))
        repo.add(
            active(
                "Stale preference nobody has confirmed", last_confirmed_at=NOW - timedelta(days=100)
            )
        )
        repo.add(
            active(
                "Dead preference nobody has confirmed", last_confirmed_at=NOW - timedelta(days=400)
            )
        )
        sweep = repo.sweep_decay(now=NOW)
        assert sweep.examined == 3
        assert sweep.decayed == 2
        assert sweep.went_dormant == 1
        assert len(sweep.due_for_review) == 2
        assert len(repo.active()) == 2

    def test_due_for_reconfirmation_lists_the_stale_ones(self, repo: ProfileRepository) -> None:
        repo.add(active("Fresh preference that was confirmed recently"))
        stale = repo.add(
            active("Stale preference nobody confirmed", last_confirmed_at=NOW - timedelta(days=100))
        )
        due = repo.due_for_reconfirmation(now=NOW)
        assert [s.id for s in due] == [stale.id]

    def test_supersession_through_the_repository_persists_both_sides(
        self, repo: ProfileRepository
    ) -> None:
        old = repo.add(active("Prefers Firebase for backend-as-a-service", entities=["firebase"]))
        replacement = ProfileStatement(
            statement="Prefers Supabase; moved off Firebase over pricing at scale",
            category=Category.TOOL_PREFERENCE,
            confidence=0.9,
            entities=["supabase"],
        )
        retired, new = repo.supersede(old.id, replacement, now=NOW)
        assert repo.get(retired.id) is not None
        assert repo.get(retired.id).status is StatementStatus.SUPERSEDED  # type: ignore[union-attr]
        assert [s.id for s in repo.active()] == [new.id]

    def test_stats_summarise_the_whole_profile(self, repo: ProfileRepository) -> None:
        seed_profile(repo, now=NOW)
        stats = repo.stats()
        assert stats.total == 50
        assert stats.by_status["proposed"] == 50
        assert stats.by_category["tool_preference"] == 20
        assert 0.8 < stats.mean_confidence < 1.0

    def test_operating_on_a_missing_statement_raises(self, repo: ProfileRepository) -> None:
        from uuid import uuid4

        with pytest.raises(KeyError):
            repo.confirm(uuid4())
