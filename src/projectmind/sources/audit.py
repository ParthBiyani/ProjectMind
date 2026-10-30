"""Re-apply today's rules to what is already indexed.

The reader's rules (skip identity documents, drop credentials and personal
identifiers) apply as files are read. A file that has not changed is not read
again, so a rule added later would never reach what was indexed before it. The
audit closes that gap: it walks the index and removes whatever the current rules
would not allow in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from uuid import UUID

from projectmind.sources.secrets import contains_secret, is_sensitive_name
from projectmind.storage import Store


@dataclass(slots=True)
class AuditReport:
    documents_checked: int = 0
    chunks_checked: int = 0
    sensitive_documents: list[str] = field(default_factory=list)
    flagged_chunks: int = 0
    removed_documents: int = 0
    removed_chunks: int = 0

    @property
    def clean(self) -> bool:
        return not self.sensitive_documents and not self.flagged_chunks


def audit(store: Store, *, fix: bool = False) -> AuditReport:
    report = AuditReport()

    for document in store.list_documents(limit=1_000_000):
        report.documents_checked += 1
        if document.source_type in {"web", "commit"}:
            continue  # URLs and commit links are not file names
        parts = PurePosixPath(document.uri).parts
        if any(is_sensitive_name(part) for part in parts[-6:]):
            report.sensitive_documents.append(document.uri)
            if fix and store.delete_document(document.uri):
                report.removed_documents += 1

    flagged: list[UUID] = []
    for chunk in store.iter_chunks():
        report.chunks_checked += 1
        if contains_secret(f"{chunk.heading}\n{chunk.text}"):
            flagged.append(chunk.id)
    report.flagged_chunks = len(flagged)
    if fix and flagged:
        report.removed_chunks = store.delete_chunks(flagged)
    return report
