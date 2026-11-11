from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from projectmind.sources.audit import audit, describe
from projectmind.sources.browser import BrowserConfig
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

    def test_a_page_from_a_site_now_on_the_never_capture_list_is_found_and_removed(
        self, store: Store
    ) -> None:
        add(store, "https://mail.google.com/mail/u/0", "Inbox", source_type="web")
        add(store, "https://docs.python.org/3/library/re.html", "re docs", source_type="web")
        browser = BrowserConfig(enabled=True)
        report = audit(store, browser=browser)
        assert report.refused_pages == ["https://mail.google.com/mail/u/0"]
        assert not report.clean
        assert audit(store, fix=True, browser=browser).removed_documents == 1
        assert store.get_document("https://mail.google.com/mail/u/0") is None
        assert store.get_document("https://docs.python.org/3/library/re.html") is not None

    def test_the_open_tabs_list_is_not_mistaken_for_a_page(self, store: Store) -> None:
        add(store, "browser://open-tabs", "Open in the browser now:", source_type="web")
        assert audit(store, browser=BrowserConfig(enabled=True)).clean

    def test_a_wrapped_tracking_link_is_refused_even_without_a_config(self, store: Store) -> None:
        add(
            store,
            "https://t.example.org/L0/https:%2F%2Fa.example.com%2Fx%3Ftoken=ab/1",
            "Assessment",
            source_type="web",
        )
        assert len(audit(store).refused_pages) == 1

    def test_the_report_says_where_a_flagged_passage_is_without_showing_it(
        self, store: Store
    ) -> None:
        add(store, "D:/p/forms.md", "My PAN is ABCDE1234F and I keep it.", "Fine text here.")
        report = audit(store)
        assert report.flagged_in == ["D:/p/forms.md"]
        assert "ABCDE1234F" not in " ".join(report.flagged_in)

    def test_a_long_address_is_shortened_for_display(self) -> None:
        shown = describe("https://example.org/" + "a" * 200 + "?q=1")
        assert shown.startswith("example.org/aaa")
        assert len(shown) <= 70
        assert "q=1" not in shown

    def test_urls_and_commit_links_are_not_judged_as_file_names(self, store: Store) -> None:
        add(
            store,
            "https://example.org/docs/passport-validation-guide",
            "How a library validates passport fields.",
            source_type="web",
        )
        assert audit(store).sensitive_documents == []
