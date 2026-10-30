"""One pass over every source: the thing that keeps the knowledge base growing.

`sync` is what you run by hand, what the daemon runs on a timer, and what a
session-start hook can trigger. It reads git history incrementally (only commits
since the last pass, with a margin) and re-scans the configured folders, which
skips anything unchanged.

It never writes into a source, never touches version control state, and leaves
recently modified files for the next pass.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from projectmind.config import Settings
from projectmind.ingestion.pipeline import IngestionPipeline, IngestionReport
from projectmind.logging import get_logger
from projectmind.models import utcnow
from projectmind.sources.browser import BrowserConfig, BrowserReport, index_history
from projectmind.sources.config import SourcesConfig
from projectmind.sources.indexer import DEFAULT_MIN_AGE_SECONDS, KnowledgeIndexer, ScanReport
from projectmind.sources.records import index_records
from projectmind.storage import Store

log = get_logger(__name__)

STATE_FILE = "sync_state.json"

#: Commits are re-read from this far before the last pass. Authored dates are not
#: commit order, and a rebased or cherry-picked commit can carry an older date.
SINCE_MARGIN = timedelta(days=2)


@dataclass(slots=True)
class SyncReport:
    git: list[IngestionReport] = field(default_factory=list)
    scans: list[ScanReport] = field(default_factory=list)
    browser: BrowserReport | None = None
    seconds: float = 0.0

    @property
    def records_stored(self) -> int:
        return sum(report.records_stored for report in self.git)

    @property
    def files_indexed(self) -> int:
        return sum(report.indexed for report in self.scans)

    def summary(self) -> str:
        pages = f", {self.browser.indexed} pages" if self.browser else ""
        return (
            f"{self.records_stored} new git records, {self.files_indexed} files read{pages} "
            f"in {self.seconds:.1f}s"
        )


def resolve_authors(settings: Settings) -> list[str]:
    """Emails whose commits are yours: configured ones plus git's `user.email`."""
    found = [email.lower() for email in settings.authors]
    try:
        configured = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        configured = ""
    if configured and configured.lower() not in found:
        found.append(configured.lower())
    return found


def _load_state(home: Path) -> dict[str, str]:
    try:
        raw = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(home: Path, state: dict[str, str]) -> None:
    try:
        (home / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save sync state", extra={"error": str(exc)})


def sync(
    store: Store,
    settings: Settings,
    *,
    git: bool = True,
    files: bool = True,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
) -> SyncReport:
    started = time.perf_counter()
    report = SyncReport()
    config = SourcesConfig.load(settings.home)
    state = _load_state(settings.home)
    began = utcnow()

    if git:
        pipeline = IngestionPipeline(store, settings)
        authors = resolve_authors(settings)
        for root in config.roots:
            if root.kind == "transcripts" or not root.path.is_dir():
                continue
            last = state.get(f"git:{root.key}")
            since = datetime.fromisoformat(last).astimezone(UTC) - SINCE_MARGIN if last else None
            report.git.append(
                pipeline.ingest_tree(
                    root.path, authors=authors, since=since, max_depth=max(1, root.depth + 1)
                )
            )
            state[f"git:{root.key}"] = began.isoformat()

    if git:
        index_records(store)

    if files:
        indexer = KnowledgeIndexer(store, min_age_seconds=min_age_seconds)
        report.scans = [indexer.scan_root(root) for root in config.roots]

    browser = BrowserConfig.load(settings.home)
    if files and browser.enabled:
        report.browser = index_history(store, browser)

    _save_state(settings.home, state)
    report.seconds = round(time.perf_counter() - started, 2)
    log.info("sync complete", extra={"summary": report.summary()})
    return report
