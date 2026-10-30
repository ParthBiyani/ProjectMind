"""Commit-derived records, made searchable like every other source.

Decisions, failures and reversals extracted from git history live in their own
table and were reachable only through a gate keyed on a closed list of
technology names. A question about your own project ("why did we drop the profit
factor column?") named nothing on that list, so the answer was never retrieved.

Mirroring each record into the knowledge base as a small document fixes that
without changing how records are stored: they are found by the same open
vocabulary, rarity-weighted matching as notes and logs, and cited by the commit
URL they came from.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from projectmind.models import EpisodicRecord, Project
from projectmind.storage import ChunkRecord, DocumentRecord, RecordFilter, Store

ROOT_KEY = "git-records"


def record_text(record: EpisodicRecord) -> str:
    """The record as the author left it: what, and only the fields that exist."""
    lines = [f"[{record.type}] {record.what}"]
    if record.why:
        lines.append(f"why: {record.why}")
    if record.outcome:
        lines.append(f"outcome: {record.outcome}")
    if record.rejected_alternatives:
        lines.append("rejected: " + "; ".join(record.rejected_alternatives))
    return "\n".join(lines)


def index_records(store: Store) -> tuple[int, int]:
    """Mirror every episodic record into the knowledge base. Returns (written, removed)."""
    projects: dict[object, Project] = {p.id: p for p in store.list_projects()}
    known = store.document_hashes(ROOT_KEY)
    seen: set[str] = set()
    written = 0

    for record in store.list_records(RecordFilter(include_superseded=True)):
        uri = record.source_url
        project = projects.get(record.project_id)
        if not uri or project is None:
            continue
        seen.add(uri)
        text = record_text(record)
        fingerprint = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
        if known.get(uri) == fingerprint:
            continue
        now = datetime.now(UTC)
        document = DocumentRecord(
            id=uuid4(),
            uri=uri,
            source_type="commit",
            root=ROOT_KEY,
            project_key=project.key,
            title=record.what[:120],
            content_hash=fingerprint,
            mtime=record.occurred_at,
            indexed_at=now,
        )
        store.replace_document(
            document,
            [
                ChunkRecord(
                    id=uuid4(),
                    document_id=document.id,
                    ordinal=0,
                    text=text,
                    heading=f"{record.type} {record.occurred_at:%Y-%m-%d}",
                    locator=f"{record.occurred_at:%Y-%m-%d}",
                )
            ],
        )
        written += 1

    removed = sum(int(store.delete_document(uri)) for uri in known.keys() - seen)
    return written, removed
