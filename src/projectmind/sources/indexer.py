"""Reading a root into the knowledge base.

A scan walks the root, skips what has not changed, and re-reads what has. It is
safe to run as often as you like: unchanged files cost a `stat`, and files being
written right now are left for the next pass.

Nothing here writes to a source. The only database written is ProjectMind's own.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from projectmind.logging import get_logger
from projectmind.sources import chunking, office, transcripts
from projectmind.sources.config import RootConfig
from projectmind.sources.models import SourceChunk
from projectmind.sources.secrets import contains_secret
from projectmind.sources.walker import (
    LOG_TAIL_BYTES,
    Candidate,
    ProjectResolver,
    project_key_for_directory,
    walk,
)
from projectmind.storage import ChunkRecord, DocumentRecord, Store

log = get_logger(__name__)

#: A file touched more recently than this is probably still being edited, and is
#: left for the next pass. It also keeps a scan from racing a running agent.
DEFAULT_MIN_AGE_SECONDS = 1_800.0


@dataclass(slots=True)
class ScanReport:
    root: str
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    too_recent: int = 0
    empty: int = 0
    removed: int = 0
    failed: int = 0
    chunks: int = 0
    withheld: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    missing: Counter[str] = field(default_factory=Counter)
    error: str | None = None
    seconds: float = 0.0

    def summary(self) -> str:
        if self.error:
            return f"{self.root}: {self.error}"
        return (
            f"{self.root}: {self.indexed} indexed, {self.unchanged} unchanged, "
            f"{self.too_recent} too recent, {self.removed} removed, {self.chunks} chunks"
        )


def fingerprint(candidate: Candidate) -> str:
    """Cheap change detector: size and modification time, no content read."""
    return f"{candidate.size}-{candidate.mtime_ns}"


def _read_text(candidate: Candidate) -> str:
    with candidate.path.open("rb") as handle:
        if candidate.source_type == "log" and candidate.size > LOG_TAIL_BYTES:
            handle.seek(-LOG_TAIL_BYTES, 2)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _title_of(candidate: Candidate, chunks: list[SourceChunk]) -> str:
    for chunk in chunks:
        first = chunk.text.splitlines()[0] if chunk.text else ""
        if first.startswith("# "):
            return first[2:].strip()
    return candidate.path.name


class KnowledgeIndexer:
    def __init__(
        self,
        store: Store,
        *,
        min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.min_age = min_age_seconds
        self.clock = clock

    # --- one root ----------------------------------------------------------
    def scan_root(self, root: RootConfig) -> ScanReport:
        started = time.perf_counter()
        report = ScanReport(root=root.key)
        if not root.path.is_dir():
            # An unplugged drive must not look like "everything was deleted".
            report.error = "folder not found; nothing was changed"
            return report

        known = self.store.document_hashes(root.key)
        resolver = ProjectResolver(root)
        seen: set[str] = set()

        found = transcripts.candidates(root.path) if root.kind == "transcripts" else walk(root.path)
        for candidate in found:
            report.seen += 1
            uri = candidate.path.as_posix()
            seen.add(uri)
            current = fingerprint(candidate)
            if known.get(uri) == current:
                report.unchanged += 1
                continue
            if self.clock() - candidate.mtime < self.min_age:
                report.too_recent += 1
                continue
            try:
                self._index(candidate, uri, current, root, resolver, report)
            except office.MissingDependencyError as exc:
                report.missing[str(exc)] += 1
            except Exception as exc:  # one unreadable file never stops a scan
                report.failed += 1
                log.warning("could not index", extra={"uri": uri, "error": str(exc)})

        for uri in known.keys() - seen:
            if self.store.delete_document(uri):
                report.removed += 1

        report.seconds = round(time.perf_counter() - started, 2)
        log.info("scanned root", extra={"summary": report.summary()})
        return report

    # --- one file ----------------------------------------------------------
    def _index(
        self,
        candidate: Candidate,
        uri: str,
        current: str,
        root: RootConfig,
        resolver: ProjectResolver,
        report: ScanReport,
    ) -> None:
        chunks, title, working_dir = self._read(candidate)
        kept = [chunk for chunk in chunks if not contains_secret(f"{chunk.heading}\n{chunk.text}")]
        report.withheld += len(chunks) - len(kept)
        if not kept:
            # Remember the file so it is not re-read until it changes, but keep
            # nothing searchable from it.
            report.empty += 1

        now = datetime.now(UTC)
        document = DocumentRecord(
            id=uuid4(),
            uri=uri,
            source_type=candidate.source_type,
            root=root.key,
            project_key=(
                project_key_for_directory(Path(working_dir))
                if working_dir
                else resolver.key_for(candidate.path)
            ),
            title=title or candidate.path.name,
            content_hash=current,
            mtime=datetime.fromtimestamp(candidate.mtime, UTC),
            indexed_at=now,
        )
        records = [
            ChunkRecord(
                id=uuid4(),
                document_id=document.id,
                ordinal=index,
                text=chunk.text,
                heading=chunk.heading,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                locator=chunk.locator,
            )
            for index, chunk in enumerate(kept)
        ]
        self.store.replace_document(document, records)
        report.indexed += 1
        report.chunks += len(records)
        report.by_type[candidate.source_type] += 1

    def _read(self, candidate: Candidate) -> tuple[list[SourceChunk], str, str | None]:
        kind = candidate.source_type
        if kind == "transcript":
            title, working_dir, session = transcripts.chunks_for(candidate.path)
            return session, title, working_dir
        if kind in office.READERS:
            title, chunks = office.READERS[kind](candidate.path)
            return chunks, title, None
        text = _read_text(candidate)
        if kind == "doc":
            suffix = candidate.path.suffix.lower()
            chunks = (
                chunking.chunk_markdown(text)
                if suffix in {".md", ".markdown"}
                else chunking.chunk_text(text)
            )
        elif kind == "code":
            chunks = chunking.chunk_python(text)
        elif kind == "notebook":
            chunks = chunking.chunk_notebook(text)
        elif kind == "log":
            chunks = chunking.chunk_log(text)
        elif kind == "results":
            chunks = chunking.chunk_results_json(text)
        else:
            chunks = []
        return chunks, _title_of(candidate, chunks), None


def scan_all(
    store: Store, roots: list[RootConfig], *, min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS
) -> list[ScanReport]:
    indexer = KnowledgeIndexer(store, min_age_seconds=min_age_seconds)
    return [indexer.scan_root(root) for root in roots]
