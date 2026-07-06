"""One suite, both backends.

Every test here runs twice: once against SQLite and once against Postgres when
`PROJECTMIND_TEST_DB_URL` is set. That is the only thing keeping the two
implementations from drifting, and drift in a storage layer shows up as a
retrieval bug three modules away.

    pytest                  # sqlite; postgres params skip
    pytest -m postgres      # postgres only, as CI runs it
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind.models import (
    Category,
    EpisodicRecord,
    EvidenceRef,
    Fingerprint,
    ProfileStatement,
    Project,
    RecordType,
    Resolution,
    ReviewItem,
    ReviewKind,
    SourceType,
    StatementStatus,
)
from projectmind.storage import RecordFilter, StatementFilter, Store
from projectmind.storage.base import BundleLogEntry
from projectmind.storage.sqlite_store import SqliteStore

PG_URL_ENV = "PROJECTMIND_TEST_DB_URL"


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ]
)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    if request.param == "sqlite":
        backend: Store = SqliteStore(tmp_path / "parity.db")
        backend.migrate()
        yield backend
        backend.close()
        return

    url = os.getenv(PG_URL_ENV)
    if not url:
        pytest.skip(f"set {PG_URL_ENV} to run the postgres half of the parity suite")

    from projectmind.storage.postgres_store import PostgresStore

    backend = PostgresStore(url)
    # Test-only reset so each parity test starts from an empty schema.
    backend._execute("DROP SCHEMA public CASCADE")
    backend._execute("CREATE SCHEMA public")
    backend.migrate()
    yield backend
    backend.close()


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def make_project(key: str = "kairos", **overrides: object) -> Project:
    defaults: dict[str, object] = {
        "key": key,
        "name": key.title(),
        "git_remote": f"https://example.invalid/{key}.git",
        "fingerprint": Fingerprint(
            languages=["Python"],
            frameworks=["FastAPI"],
            dependencies=["fastapi", "redis", "pydantic"],
            manifest_hash="hash-1",
        ),
    }
    defaults.update(overrides)
    return Project.model_validate(defaults)


def make_record(project_id: object, **overrides: object) -> EpisodicRecord:
    defaults: dict[str, object] = {
        "project_id": project_id,
        "type": RecordType.FAILURE,
        "what": "CUDA out of memory during validation",
        "why": "validation image size was larger than training",
        "rejected_alternatives": ["gradient accumulation"],
        "outcome": "pinned the validation image size",
        "entities": ["CUDA", "yolov8"],
        "source_type": SourceType.COMMIT,
        "source_url": "fixture://repo/commit/abc",
        "occurred_at": datetime(2025, 10, 29, tzinfo=UTC),
        "confidence": 0.89,
        "is_inference": False,
    }
    defaults.update(overrides)
    return EpisodicRecord.model_validate(defaults)


def make_statement(**overrides: object) -> ProfileStatement:
    defaults: dict[str, object] = {
        "statement": "Prefers Supabase for backend-as-a-service",
        "category": Category.TOOL_PREFERENCE,
        "confidence": 0.9,
        "status": StatementStatus.ACTIVE,
        "entities": ["supabase"],
        "evidence_refs": [EvidenceRef(kind=SourceType.COMMIT, url="fixture://x", label="x")],
    }
    defaults.update(overrides)
    return ProfileStatement.model_validate(defaults)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestMigrations:
    def test_migrate_is_idempotent(self, store: Store) -> None:
        assert store.migrate() == 1
        assert store.migrate() == 1


class TestProjects:
    def test_roundtrip_preserves_the_fingerprint(self, store: Store) -> None:
        stored = store.upsert_project(make_project())
        fetched = store.get_project("kairos")
        assert fetched is not None
        assert fetched.id == stored.id
        assert fetched.fingerprint.dependencies == ("fastapi", "pydantic", "redis")
        assert fetched.fingerprint.languages == ("python",)

    def test_upsert_keeps_the_original_id_and_first_seen(self, store: Store) -> None:
        first = store.upsert_project(make_project())
        again = store.upsert_project(make_project(name="Renamed"))
        assert again.id == first.id
        assert again.first_seen == first.first_seen
        assert again.name == "Renamed"
        assert len(store.list_projects()) == 1

    def test_lookup_by_id(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        assert store.get_project_by_id(project.id) is not None
        assert store.get_project_by_id(uuid4()) is None

    def test_missing_project_is_none(self, store: Store) -> None:
        assert store.get_project("nope") is None


class TestProfileStatements:
    def test_roundtrip_preserves_every_lifecycle_field(self, store: Store) -> None:
        statement = make_statement(contradiction_count=2, contradiction_refs=["a", "b"])
        store.upsert_statement(statement)
        fetched = store.get_statement(statement.id)
        assert fetched is not None
        assert fetched.statement == statement.statement
        assert fetched.category is Category.TOOL_PREFERENCE
        assert fetched.ttl_days == 90
        assert fetched.contradiction_count == 2
        assert fetched.contradiction_refs == ("a", "b")
        assert fetched.evidence_refs[0].url == "fixture://x"

    def test_scope_survives_a_keep_both_resolution(self, store: Store) -> None:
        statement = make_statement(scope={"when": "throwaway prototypes"})
        store.upsert_statement(statement)
        fetched = store.get_statement(statement.id)
        assert fetched is not None
        assert fetched.scope == {"when": "throwaway prototypes"}

    def test_status_filter_defaults_to_active_only(self, store: Store) -> None:
        store.upsert_statement(make_statement())
        store.upsert_statement(make_statement(status=StatementStatus.DORMANT, entities=["x"]))
        assert len(store.list_statements()) == 1
        assert len(store.list_statements(StatementFilter(statuses=()))) == 2

    def test_category_and_entity_filters(self, store: Store) -> None:
        store.upsert_statement(make_statement())
        store.upsert_statement(
            make_statement(
                statement="Writes tests after the feature",
                category=Category.WORK_STYLE,
                entities=["pytest"],
            )
        )
        by_category = store.list_statements(StatementFilter(categories=(Category.WORK_STYLE,)))
        assert [s.category for s in by_category] == [Category.WORK_STYLE]
        by_entity = store.list_statements(StatementFilter(entities=("supabase",)))
        assert len(by_entity) == 1

    def test_update_in_place(self, store: Store) -> None:
        statement = make_statement()
        store.upsert_statement(statement)
        store.upsert_statement(
            statement.model_copy(update={"status": StatementStatus.SUPERSEDED, "confidence": 0.4})
        )
        fetched = store.get_statement(statement.id)
        assert fetched is not None
        assert fetched.status is StatementStatus.SUPERSEDED
        assert fetched.confidence == pytest.approx(0.4)

    def test_delete(self, store: Store) -> None:
        statement = make_statement()
        store.upsert_statement(statement)
        assert store.delete_statement(statement.id) is True
        assert store.delete_statement(statement.id) is False


class TestEpisodicRecords:
    def test_roundtrip(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        record = make_record(project.id)
        assert store.add_records([record]) == 1
        fetched = store.get_record(record.id)
        assert fetched is not None
        assert fetched.what == record.what
        assert fetched.entities == ("cuda", "yolov8")
        assert fetched.rejected_alternatives == ("gradient accumulation",)
        assert fetched.is_inference is False
        assert fetched.occurred_at == record.occurred_at

    def test_content_hash_deduplicates_repeat_ingestion(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        record = make_record(project.id)
        assert store.add_records([record]) == 1
        assert store.add_records([record]) == 0
        assert store.count_records() == 1

    def test_entity_filter_is_an_index_lookup(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        store.add_records(
            [
                make_record(project.id),
                make_record(project.id, what="Chose ONNX for CPU inference", entities=["onnx"]),
            ]
        )
        assert len(store.list_records(RecordFilter(entities=("cuda",)))) == 1
        assert len(store.list_records(RecordFilter(entities=("onnx",)))) == 1
        assert len(store.list_records(RecordFilter(entities=("cuda", "onnx")))) == 2
        assert store.list_records(RecordFilter(entities=("nothing",))) == []

    def test_project_include_and_exclude_filters(self, store: Store) -> None:
        one = store.upsert_project(make_project("one"))
        two = store.upsert_project(make_project("two"))
        store.add_records([make_record(one.id), make_record(two.id, what="Other thing")])
        assert len(store.list_records(RecordFilter(project_ids=(one.id,)))) == 1
        assert len(store.list_records(RecordFilter(exclude_project_ids=(one.id,)))) == 1

    def test_type_and_confidence_filters(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        store.add_records(
            [
                make_record(project.id),
                make_record(
                    project.id,
                    what="Adopted Supabase",
                    type=RecordType.DECISION,
                    confidence=0.5,
                ),
            ]
        )
        assert len(store.list_records(RecordFilter(types=(RecordType.DECISION,)))) == 1
        assert len(store.list_records(RecordFilter(min_confidence=0.7))) == 1

    def test_occurred_after_filter(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        store.add_records(
            [
                make_record(project.id),
                make_record(
                    project.id,
                    what="Recent thing",
                    occurred_at=datetime(2026, 5, 1, tzinfo=UTC),
                ),
            ]
        )
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)
        assert len(store.list_records(RecordFilter(occurred_after=cutoff))) == 1

    def test_superseded_records_are_hidden_by_default(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        current = make_record(project.id, what="Current approach")
        store.add_records([current])
        store.add_records([make_record(project.id, what="Old approach", superseded_by=current.id)])
        assert [r.what for r in store.list_records()] == ["Current approach"]
        both = store.list_records(RecordFilter(include_superseded=True))
        assert {r.what for r in both} == {"Current approach", "Old approach"}

    def test_results_are_ordered_newest_first(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        store.add_records(
            [
                make_record(project.id, what="older", occurred_at=datetime(2025, 1, 1, tzinfo=UTC)),
                make_record(project.id, what="newer", occurred_at=datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        )
        assert [r.what for r in store.list_records()] == ["newer", "older"]

    def test_limit_is_applied(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        store.add_records([make_record(project.id, what=f"thing {i}") for i in range(5)])
        assert len(store.list_records(RecordFilter(limit=2))) == 2


class TestVectorSearch:
    def test_nearest_neighbour_ranks_the_closest_vector_first(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        near = [1.0, 0.0, 0.0] + [0.0] * 381
        far = [0.0, 1.0, 0.0] + [0.0] * 381
        store.add_records(
            [
                make_record(project.id, what="near", embedding=near),
                make_record(project.id, what="far", embedding=far),
            ]
        )
        results = store.vector_search(near, limit=2)
        assert [r.record.what for r in results] == ["near", "far"]
        assert results[0].similarity > results[1].similarity
        assert results[0].similarity == pytest.approx(1.0, abs=1e-4)

    def test_vector_search_honours_record_filters(self, store: Store) -> None:
        one = store.upsert_project(make_project("one"))
        two = store.upsert_project(make_project("two"))
        vector = [1.0] + [0.0] * 383
        store.add_records(
            [
                make_record(one.id, what="in one", embedding=vector),
                make_record(two.id, what="in two", embedding=vector),
            ]
        )
        results = store.vector_search(vector, limit=5, filters=RecordFilter(project_ids=(two.id,)))
        assert [r.record.what for r in results] == ["in two"]

    def test_records_without_embeddings_are_not_returned(self, store: Store) -> None:
        project = store.upsert_project(make_project())
        store.add_records([make_record(project.id, what="no vector")])
        assert store.vector_search([1.0] + [0.0] * 383) == []


class TestFingerprintCache:
    def test_put_get_invalidate(self, store: Store) -> None:
        fingerprint = Fingerprint(languages=["dart"], manifest_hash="h1")
        store.put_cached_fingerprint("spendwise", fingerprint)
        cached = store.get_cached_fingerprint("spendwise")
        assert cached is not None
        assert cached[0] == "h1"
        assert cached[1].languages == ("dart",)
        store.invalidate_fingerprint("spendwise")
        assert store.get_cached_fingerprint("spendwise") is None

    def test_overwrites_on_manifest_change(self, store: Store) -> None:
        store.put_cached_fingerprint("x", Fingerprint(manifest_hash="h1"))
        store.put_cached_fingerprint("x", Fingerprint(manifest_hash="h2"))
        cached = store.get_cached_fingerprint("x")
        assert cached is not None and cached[0] == "h2"


class TestReviewQueue:
    def test_open_items_are_ranked_by_impact(self, store: Store) -> None:
        low = ReviewItem(
            kind=ReviewKind.RECONFIRMATION, target_id=uuid4(), proposal="low", impact=0.2
        )
        high = ReviewItem(
            kind=ReviewKind.SUPERSESSION, target_id=uuid4(), proposal="high", impact=0.9
        )
        store.enqueue_review(low)
        store.enqueue_review(high)
        assert [item.proposal for item in store.list_reviews()] == ["high", "low"]

    def test_resolving_removes_it_from_the_open_list_but_keeps_the_record(
        self, store: Store
    ) -> None:
        item = ReviewItem(kind=ReviewKind.SUPERSESSION, target_id=uuid4(), proposal="p")
        store.enqueue_review(item)
        resolved = store.resolve_review(item.id, Resolution.REJECT, note="noise")
        assert resolved is not None
        assert resolved.resolution is Resolution.REJECT
        assert resolved.resolution_note == "noise"
        assert resolved.is_open is False
        assert store.list_reviews() == []
        assert len(store.list_reviews(open_only=False)) == 1

    def test_resolving_an_unknown_item_returns_none(self, store: Store) -> None:
        assert store.resolve_review(uuid4(), Resolution.CONFIRM) is None


class TestTelemetry:
    def _entry(self, **overrides: object) -> BundleLogEntry:
        defaults: dict[str, object] = {
            "bundle_id": uuid4(),
            "created_at": datetime.now(UTC),
            "project_key": "kairos",
            "task_type": "debug",
            "prompt_hash": "abc123",
            "prompt_words": 9,
            "injected": True,
            "profile_tokens": 200,
            "episodic_tokens": 400,
            "latency_ms": 14.0,
        }
        defaults.update(overrides)
        return BundleLogEntry(**defaults)  # type: ignore[arg-type]

    def test_bundle_roundtrip(self, store: Store) -> None:
        entry = self._entry(episodic_ids=(uuid4(),), cross_project_ids=(uuid4(),))
        store.log_bundle(entry)
        recent = store.recent_bundles()
        assert len(recent) == 1
        assert recent[0].bundle_id == entry.bundle_id
        assert recent[0].prompt_hash == "abc123"
        assert recent[0].injected is True
        assert len(recent[0].cross_project_ids) == 1

    def test_feedback_updates_an_existing_bundle(self, store: Store) -> None:
        entry = self._entry()
        store.log_bundle(entry)
        assert store.record_feedback(entry.bundle_id, useful=False, note="wrong repo") is True
        assert store.record_feedback(uuid4(), useful=True) is False
        stats = store.usage_stats()
        assert stats.feedback_negative == 1
        assert stats.observed_false_injection_rate == pytest.approx(1.0)

    def test_usage_stats_aggregate_the_things_the_report_prints(self, store: Store) -> None:
        store.log_bundle(self._entry(task_type="debug"))
        store.log_bundle(self._entry(task_type="debug", injected=False, profile_tokens=0))
        store.log_bundle(self._entry(task_type="architect", cross_project_ids=(uuid4(),)))
        stats = store.usage_stats()
        assert stats.bundles == 3
        assert stats.injected == 2
        assert stats.skipped == 1
        assert stats.cross_project_bundles == 1
        assert stats.by_task_type == {"debug": 2, "architect": 1}
        assert stats.by_project == {"kairos": 3}
        assert stats.injection_rate == pytest.approx(2 / 3)

    def test_usage_stats_can_be_windowed(self, store: Store) -> None:
        now = datetime.now(UTC)
        store.log_bundle(self._entry(created_at=now - timedelta(days=40)))
        store.log_bundle(self._entry(created_at=now))
        assert store.usage_stats(since=now - timedelta(days=7)).bundles == 1

    def test_no_bundles_means_zeroes_not_errors(self, store: Store) -> None:
        stats = store.usage_stats()
        assert stats.bundles == 0
        assert stats.injection_rate == 0.0
        assert stats.observed_false_injection_rate == 0.0
