"""Git-derived records answer questions about your own projects.

The motivating case: "Why did we drop the profit factor column?" named nothing on
the old closed vocabulary of technologies, so a commit saying exactly that was
never retrieved.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from projectmind.config import Settings
from projectmind.models import EpisodicRecord, Fingerprint, Project, RecordType, SourceType
from projectmind.service import MemoryService
from projectmind.sources.records import index_records, record_text
from projectmind.storage import SqliteStore, Store

URL = "https://github.com/acme/alpha-desk/commit/2924c633eae637652ed14f4e838c5bd8c045b69a"


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def seed(store: Store, **overrides: object) -> EpisodicRecord:
    project = store.upsert_project(
        Project(key="acme-alpha-desk", name="alpha-desk", fingerprint=Fingerprint())
    )
    fields: dict[str, object] = {
        "project_id": project.id,
        "type": RecordType.FAILURE,
        "what": "drop the profit factor column from the variations table",
        "source_type": SourceType.COMMIT,
        "source_url": URL,
        "occurred_at": datetime(2026, 10, 26, tzinfo=UTC),
        "confidence": 0.8,
        "is_inference": True,
    }
    fields.update(overrides)
    record = EpisodicRecord(**fields)  # type: ignore[arg-type]
    store.add_records([record])
    return record


def noise(store: Store, count: int = 40) -> None:
    project = store.upsert_project(Project(key="noise", name="noise", fingerprint=Fingerprint()))
    for index in range(count):
        record = EpisodicRecord(
            project_id=project.id,
            type=RecordType.DECISION,
            what=f"adjust the logging format number {index} for the worker",
            source_type=SourceType.COMMIT,
            source_url=f"https://example.invalid/c/{uuid4()}",
            occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
            confidence=0.7,
            is_inference=True,
        )
        store.add_records([record])


class TestMirroring:
    def test_a_record_becomes_a_commit_document_cited_by_its_url(self, store: Store) -> None:
        seed(store)
        written, removed = index_records(store)
        assert (written, removed) == (1, 0)
        [hit] = store.search_chunks(["profit", "factor"])
        assert hit.document.uri == URL
        assert hit.document.source_type == "commit"
        assert hit.document.project_key == "acme-alpha-desk"
        assert hit.chunk.text.startswith("[failure] drop the profit factor column")

    def test_only_the_fields_that_exist_are_written(self) -> None:
        record = EpisodicRecord(
            project_id=uuid4(),
            type=RecordType.DECISION,
            what="adopt walk-forward",
            why="k-fold leaked the future",
            source_type=SourceType.COMMIT,
            source_url=URL,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            confidence=0.9,
            is_inference=False,
        )
        assert record_text(record) == "[decision] adopt walk-forward\nwhy: k-fold leaked the future"

    def test_a_second_pass_changes_nothing(self, store: Store) -> None:
        seed(store)
        index_records(store)
        assert index_records(store) == (0, 0)

    def test_a_removed_record_leaves_the_index(self, store: Store) -> None:
        seed(store)
        index_records(store)
        store.delete_document(URL)
        assert store.search_chunks(["profit"]) == []


class TestTheQuestionThatFailedBefore:
    def memory(self, store: Store) -> MemoryService:
        seed(store)
        noise(store)
        index_records(store)
        return MemoryService(store, Settings(db_url=None, recency_half_life_days=1e6))

    def test_it_is_answered_from_the_commit(self, store: Store) -> None:
        bundle = self.memory(store).get_context(
            "Why did we drop the profit factor column?", "acme-alpha-desk", log_bundle=False
        )
        served = [item.citation for item in bundle.evidence]
        assert any(citation.startswith(URL) for citation in served)
        assert "drop the profit factor column" in bundle.render()

    def test_a_question_about_something_else_is_silent_about_it(self, store: Store) -> None:
        bundle = self.memory(store).get_context(
            "Set up kubernetes ingress certificate rotation", "acme-alpha-desk", log_bundle=False
        )
        assert bundle.evidence == ()

    def test_the_same_commit_is_never_served_twice(self, store: Store) -> None:
        bundle = self.memory(store).get_context(
            "Why did we drop the profit factor column in the variations table?",
            "acme-alpha-desk",
            log_bundle=False,
        )
        urls = [item.source_url for item in bundle.episodic] + [
            item.citation for item in bundle.evidence
        ]
        assert sum(url.startswith(URL) for url in urls) <= 1
