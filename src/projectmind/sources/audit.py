"""Re-apply today's rules to what is already indexed.

The reader's rules (skip identity documents, drop credentials and personal
identifiers, never read a site on the never-capture list) apply as files and pages
are read. A file that has not changed is not read again, so a rule added later would
never reach what was indexed before it. The audit closes that gap: it walks the index
and removes whatever the current rules would not allow in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

from projectmind.sources.browser import BrowserConfig, clean_url
from projectmind.sources.secrets import contains_secret, is_sensitive_name
from projectmind.storage import Store


@dataclass(slots=True)
class AuditReport:
    documents_checked: int = 0
    chunks_checked: int = 0
    sensitive_documents: list[str] = field(default_factory=list)
    #: Web pages from a site that is now never captured, a sign-in page, or a wrapped link.
    refused_pages: list[str] = field(default_factory=list)
    flagged_chunks: int = 0
    #: Where the flagged passages live, one entry per document (secrets are never shown).
    flagged_in: list[str] = field(default_factory=list)
    removed_documents: int = 0
    removed_chunks: int = 0

    @property
    def clean(self) -> bool:
        return not self.sensitive_documents and not self.flagged_chunks and not self.refused_pages


def describe(uri: str, width: int = 70) -> str:
    """A location safe to print: a web address is cut to host and the start of its path."""
    if uri.startswith(("http://", "https://")):
        parts = urlsplit(uri)
        shown = f"{parts.hostname}{parts.path}"
        return shown if len(shown) <= width else shown[: width - 1] + "…"
    return uri


def audit(store: Store, *, fix: bool = False, browser: BrowserConfig | None = None) -> AuditReport:
    report = AuditReport()
    locations: dict[UUID, str] = {}

    for document in store.list_documents(limit=1_000_000):
        report.documents_checked += 1
        locations[document.id] = document.uri
        if document.source_type == "web":
            if _refused_page(document.uri, browser):
                report.refused_pages.append(document.uri)
                if fix and store.delete_document(document.uri):
                    report.removed_documents += 1
            continue
        if document.source_type == "commit":
            continue  # commit links are not file names
        parts = PurePosixPath(document.uri).parts
        if any(is_sensitive_name(part) for part in parts[-6:]):
            report.sensitive_documents.append(document.uri)
            if fix and store.delete_document(document.uri):
                report.removed_documents += 1

    flagged: list[UUID] = []
    places: dict[str, None] = {}
    for chunk in store.iter_chunks():
        report.chunks_checked += 1
        if contains_secret(f"{chunk.heading}\n{chunk.text}"):
            flagged.append(chunk.id)
            places[locations.get(chunk.document_id, str(chunk.document_id))] = None
    report.flagged_chunks = len(flagged)
    report.flagged_in = list(places)
    if fix and flagged:
        report.removed_chunks = store.delete_chunks(flagged)
    return report


def _refused_page(uri: str, browser: BrowserConfig | None) -> bool:
    """Whether the browser reader would refuse this page today."""
    if not uri.startswith(("http://", "https://")):
        return False  # the capture module's own `browser://open-tabs` list, not a page
    if clean_url(uri) is None:
        return True
    if browser is None:
        return False
    return not browser.permits_page(uri)
