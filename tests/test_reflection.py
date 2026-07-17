from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from projectmind.config import Settings
from projectmind.models import (
    Category,
    EpisodicRecord,
    Origin,
    ProfileStatement,
    Project,
    RecordType,
    Resolution,
    ReviewKind,
    SourceType,
    StatementStatus,
)
from projectmind.profile.repository import ProfileRepository
from projectmind.reflection import ReflectionLoop, check, detect, propose, scan, subject_entities
from projectmind.storage import SqliteStore, Store

NOW = datetime(2026, 7, 16, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path)


@pytest.fixture
def profile(store: Store, settings: Settings) -> ProfileRepository:
    return ProfileRepository(store, settings)


@pytest.fixture
def project(store: Store) -> Project:
    return store.upsert_project(Project(key="demo", name="Demo"))


def statement(text: str, **overrides: object) -> ProfileStatement:
    payload: dict[str, object] = {
        "statement": text,
        "category": Category.TOOL_PREFERENCE,
        "confidence": 0.9,
        "status": StatementStatus.ACTIVE,
        "last_confirmed_at": NOW,
        "created_at": NOW - timedelta(days=300),
    }
    payload.update(overrides)
    return ProfileStatement.model_validate(payload)


def record(what: str, project: Project, **overrides: object) -> EpisodicRecord:
    payload: dict[str, object] = {
        "project_id": project.id,
        "type": RecordType.DECISION,
        "what": what,
        "source_type": SourceType.COMMIT,
        "source_url": "fixture://x",
        "occurred_at": NOW - timedelta(days=10),
        "confidence": 0.9,
        "is_inference": False,
    }
    payload.update(overrides)
    return EpisodicRecord.model_validate(payload)


class TestSubjectEntities:
    def test_a_preference_is_about_what_it_names(self) -> None:
        subject = subject_entities(
            statement("Uses Supabase for backend-as-a-service", entities=["supabase", "firebase"])
        )
        assert subject == frozenset({"supabase"})

    def test_a_contrast_entity_is_not_the_subject(self) -> None:
        """This confusion made "adopted Supabase instead of Firebase" read as
        contradicting "uses Supabase"."""
        subject = subject_entities(
            statement(
                "Uses Supabase for backend-as-a-service",
                entities=["supabase", "firebase", "appwrite"],
            )
        )
        assert "firebase" not in subject

    def test_an_abandonment_is_about_the_thing_abandoned(self) -> None:
        subject = subject_entities(
            statement(
                "Abandoned k-fold cross-validation on time series in favour of walk-forward",
                category=Category.ABANDONED,
                entities=["kfold", "cross-validation", "walk-forward", "time-series"],
            )
        )
        assert "walk-forward" not in subject

    def test_an_abandonment_is_not_about_where_it_happened(self) -> None:
        subject = subject_entities(
            statement(
                "Abandoned Provider for Flutter state management after rebuild scoping",
                category=Category.ABANDONED,
                entities=["provider", "flutter", "riverpod"],
            )
        )
        assert subject == frozenset({"provider"})

    def test_a_statement_naming_nothing_falls_back_to_its_whole_list(self) -> None:
        assert subject_entities(statement("Prefers small pull requests", entities=["git"])) == (
            frozenset({"git"})
        )


class TestContradictionRules:
    def test_a_record_rejecting_the_preferred_tool_contradicts_it(self, project: Project) -> None:
        found = check(
            statement("Prefers Firebase for backend-as-a-service", entities=["firebase"]),
            record("Chose Supabase", project, rejected_alternatives=["firebase"]),
        )
        assert found is not None
        assert found.rule == "rejected"

    def test_a_record_confirming_the_preference_does_not(self, project: Project) -> None:
        """The regression that produced six backwards proposals."""
        found = check(
            statement("Uses Supabase for auth", entities=["supabase", "firebase"]),
            record(
                "Adopted Supabase for auth",
                project,
                rejected_alternatives=["firebase"],
                entities=["supabase"],
            ),
        )
        assert found is None

    def test_a_reversal_must_be_about_the_entity_not_merely_mention_it(
        self, project: Project
    ) -> None:
        platform_only = check(
            statement("Reaches for Flutter for client-side UI", entities=["flutter"]),
            record(
                "Migrated the care-history collection off Firestore onto Supabase",
                project,
                type=RecordType.REVERSAL,
                entities=["firestore", "supabase", "flutter"],
            ),
        )
        assert platform_only is None

        about_it = check(
            statement("Reaches for Flutter for client-side UI", entities=["flutter"]),
            record(
                "Rewrote the Flutter client as a web app",
                project,
                type=RecordType.REVERSAL,
                entities=["flutter"],
            ),
        )
        assert about_it is not None and about_it.rule == "reversal"

    def test_readopting_an_abandoned_tool_contradicts_the_abandonment(
        self, project: Project
    ) -> None:
        found = check(
            statement(
                "Abandoned SMOTE for class imbalance",
                category=Category.ABANDONED,
                entities=["smote"],
            ),
            record("Used SMOTE to balance the training set", project, entities=["smote"]),
        )
        assert found is not None and found.rule == "readopted"

    def test_unrelated_records_never_contradict(self, project: Project) -> None:
        assert (
            check(
                statement("Prefers Riverpod", entities=["riverpod"]),
                record("Tuned the Postgres connection pool", project, entities=["postgres"]),
            )
            is None
        )


class TestPrefilter:
    def test_the_prefilter_removes_most_pairs(self, project: Project) -> None:
        statements = [
            statement(f"Prefers tool{i} for things", entities=[f"tool{i}"]) for i in range(20)
        ]
        records = [record(f"Did thing {i}", project, entities=[f"other{i}"]) for i in range(20)]
        _, stats = detect(statements, records)
        assert stats.pairs_possible == 400
        assert stats.pairs_after_prefilter == 0
        assert stats.prefilter_saving == 1.0

    def test_overlapping_pairs_do_survive_it(self, project: Project) -> None:
        statements = [statement("Prefers Firebase", entities=["firebase"])]
        records = [record("Chose Supabase", project, rejected_alternatives=["firebase"])]
        found, stats = detect(statements, records)
        assert stats.pairs_after_prefilter == 1
        assert stats.contradictions == 1
        assert found


class TestProposals:
    def test_a_proposal_names_what_replaced_what(self, project: Project) -> None:
        target = statement("Prefers Firebase for backend-as-a-service", entities=["firebase"])
        evidence = [
            check(
                target,
                record(
                    f"Chose Supabase on project {i}",
                    project,
                    rejected_alternatives=["firebase"],
                    entities=["supabase"],
                ),
            )
            for i in range(3)
        ]
        proposal = propose(target, [item for item in evidence if item])
        assert "supabase" in proposal.text.lower()
        assert "firebase" in proposal.text.lower()
        assert proposal.impact > 0


class TestTtlScan:
    def test_a_fresh_statement_is_left_alone(self, project: Project) -> None:
        result = scan([statement("Prefers Riverpod", entities=["riverpod"])], [], now=NOW)
        assert result.needs_review == []

    def test_supporting_evidence_refreshes_silently(self, project: Project) -> None:
        """The property that keeps the review under five minutes."""
        stale = statement(
            "Prefers Riverpod", entities=["riverpod"], last_confirmed_at=NOW - timedelta(days=120)
        )
        support = record(
            "Wired Riverpod into the new screen",
            project,
            entities=["riverpod"],
            occurred_at=NOW - timedelta(days=5),
        )
        result = scan([stale], [support], now=NOW)
        assert len(result.refreshed) == 1
        assert result.needs_review == []

    def test_no_evidence_either_way_asks_a_human(self, project: Project) -> None:
        stale = statement(
            "Prefers Riverpod", entities=["riverpod"], last_confirmed_at=NOW - timedelta(days=120)
        )
        result = scan([stale], [], now=NOW)
        assert [s.id for s in result.needs_review] == [stale.id]

    def test_three_ttls_with_nothing_goes_dormant(self, project: Project) -> None:
        dead = statement(
            "Prefers Riverpod", entities=["riverpod"], last_confirmed_at=NOW - timedelta(days=400)
        )
        result = scan([dead], [], now=NOW)
        assert len(result.went_dormant) == 1
        assert result.went_dormant[0].status is StatementStatus.DORMANT


class TestReflectionLoop:
    def _contested(
        self, profile: ProfileRepository, store: Store, project: Project
    ) -> ProfileStatement:
        target = profile.add(
            statement(
                "Prefers Firebase for backend-as-a-service on mobile projects",
                entities=["firebase"],
                last_confirmed_at=NOW - timedelta(days=200),
            )
        )
        store.add_records(
            [
                record(
                    f"Chose Supabase for project {i}",
                    project,
                    why="row level security removed client-side permission code",
                    rejected_alternatives=["firebase"],
                    entities=["supabase"],
                    occurred_at=NOW - timedelta(days=10 + i * 20),
                )
                for i in range(3)
            ]
        )
        return target

    def test_three_contradictions_open_a_proposal(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        self._contested(profile, store, project)
        state = ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        assert len(state.proposals) == 1
        assert len(state.queued) >= 1
        assert state.queued[0].kind is ReviewKind.SUPERSESSION

    def test_one_contradiction_does_not(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        """A single experimental branch must not overturn a settled preference."""
        profile.add(statement("Prefers Firebase", entities=["firebase"]))
        store.add_records(
            [record("Chose Supabase once", project, rejected_alternatives=["firebase"])]
        )
        state = ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        assert state.proposals == []

    def test_the_queued_proposal_carries_clickable_evidence(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        self._contested(profile, store, project)
        state = ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        item = state.queued[0]
        assert len(item.evidence_refs) == 3
        assert all(ref.url for ref in item.evidence_refs)

    def test_both_engines_agree(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        """LangGraph and the fallback run the same node functions."""
        self._contested(profile, store, project)
        loop = ReflectionLoop(store, settings, profile=profile)
        with_graph = loop.run(now=NOW, use_langgraph=True)
        direct = loop.run(now=NOW, use_langgraph=False)
        assert len(with_graph.proposals) == len(direct.proposals)
        assert with_graph.detection.contradictions == direct.detection.contradictions
        assert with_graph.errors == direct.errors == []

    def test_the_batch_is_persisted(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        self._contested(profile, store, project)
        ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        assert len(store.list_reviews()) >= 1

    def test_an_empty_profile_produces_an_empty_batch(
        self, store: Store, settings: Settings, profile: ProfileRepository
    ) -> None:
        state = ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        assert state.queued == []
        assert state.errors == []

    def test_a_full_supersession_end_to_end(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        """The Phase 5 exit criterion, as one test.

        Evidence accumulates, the loop proposes, a human accepts, the old
        statement stops being served, and the timeline of the change survives.
        """
        target = self._contested(profile, store, project)
        state = ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        item = state.queued[0]

        replacement = ProfileStatement(
            statement=item.proposal,
            category=Category.TOOL_PREFERENCE,
            confidence=0.9,
            entities=["supabase"],
            origin=Origin.REFLECTION,
        )
        _, new = profile.supersede(target.id, replacement, now=NOW)
        store.resolve_review(item.id, Resolution.SUPERSEDE, note="accepted")

        stored_old = profile.get(target.id)
        assert stored_old is not None
        assert stored_old.status is StatementStatus.SUPERSEDED
        assert stored_old.superseded_by == new.id
        assert stored_old.statement == target.statement, "history must survive"

        active = profile.active()
        assert [s.id for s in active] == [new.id]
        assert profile.get(new.id).origin is Origin.REFLECTION  # type: ignore[union-attr]
        assert store.list_reviews() == []

    def test_rejecting_a_contradiction_clears_it_and_resets_the_clock(
        self, store: Store, settings: Settings, profile: ProfileRepository, project: Project
    ) -> None:
        target = self._contested(profile, store, project)
        ReflectionLoop(store, settings, profile=profile).run(now=NOW)
        assert profile.get(target.id).contradiction_count == 3  # type: ignore[union-attr]

        cleared = profile.reject_contradiction(target.id, now=NOW)
        assert cleared.contradiction_count == 0
        assert cleared.last_confirmed_at == NOW
        assert cleared.status is StatementStatus.ACTIVE
