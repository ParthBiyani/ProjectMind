from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from projectmind.sources.audit import audit
from projectmind.storage import ChunkRecord, DocumentRecord, SqliteStore, Store


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def add(store: Store, uri: str, *texts: str, source_type: str = "doc") -> DocumentRecord:
    now = datetime.now(UTC)
    document = DocumentRecord(
        id=uuid4(),
        uri=uri,
        source_type=source_type,
        root="r",
        project_key="p",
        title=uri,
        content_hash="h",
        mtime=now,
        indexed_at=now,
    )
    store.replace_document(
        document,
        [
            ChunkRecord(id=uuid4(), document_id=document.id, ordinal=i, text=text)
            for i, text in enumerate(texts)
        ],
    )
    return document


class TestAudit:
    def test_a_clean_index_is_clean(self, store: Store) -> None:
        add(store, "D:/p/NOTES.md", "The roadmap covers retrieval and evaluation work.")
        report = audit(store)
        assert report.clean
        assert (report.documents_checked, report.chunks_checked) == (1, 1)

    def test_an_identity_document_indexed_before_the_rule_is_found_and_removed(
        self, store: Store
    ) -> None:
        add(store, "D:/Docs/Varun/VB. Passport.pdf", "some text from a scan")
        add(store, "D:/p/NOTES.md", "The roadmap covers retrieval.")
        assert not audit(store).clean
        report = audit(store, fix=True)
        assert report.removed_documents == 1
        assert store.search_chunks(["scan"]) == []
        assert store.search_chunks(["roadmap"])

    def test_a_folder_name_is_enough(self, store: Store) -> None:
        add(store, "D:/Docs/Personal Docs/misc/scan1.pdf", "anything at all here")
        assert audit(store, fix=True).removed_documents == 1

    def test_only_the_offending_passage_is_removed(self, store: Store) -> None:
        add(
            store,
            "D:/p/forms.md",
            "My PAN is ABCDE1234F and I keep it for filing.",
            "The quarterly roadmap covers evaluation work.",
        )
        report = audit(store, fix=True)
        assert (report.flagged_chunks, report.removed_chunks) == (1, 1)
        assert store.search_chunks(["ABCDE1234F"]) == []
        assert store.search_chunks(["roadmap"])
        document = store.get_document("D:/p/forms.md")
        assert document is not None
        assert document.chunk_count == 1

    def test_report_only_mode_changes_nothing(self, store: Store) -> None:
        add(store, "D:/Docs/VB. Passport.pdf", "text")
        audit(store)
        assert store.count_documents() == 1

    def test_urls_and_commit_links_are_not_judged_as_file_names(self, store: Store) -> None:
        add(
            store,
            "https://example.org/docs/passport-validation-guide",
            "How a library validates passport fields.",
            source_type="web",
        )
        assert audit(store).sensitive_documents == []
