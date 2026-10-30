"""Browser history from Edge and Chrome, under strict privacy rules.

Browser history is the most sensitive data on a developer's machine and mostly
has nothing to do with work. So the default is the opposite of "read it all":

* **Allow-list.** Only pages on domains you have allowed are considered. Banking,
  mail, social media, health, shopping and everything else never enter the index.
* **Nothing but the page.** URLs are stored without query string or fragment (no
  tokens, no session ids), with the page title and a visit count.
* **Searches only with a purpose.** A search query is kept only if, within a few
  minutes, you went on to a page on an allowed domain. "pgvector hnsw tuning"
  followed by the PostgreSQL docs is knowledge; a search that led nowhere you
  allowed is not stored at all.
* **A copy, read-only.** The browser's own database is copied and the copy is
  read. The original is never opened for writing.

Each stored item says what it is: `Visited <title> N times (first..last)` and,
when applicable, `Found by searching: ...`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

from projectmind.logging import get_logger
from projectmind.sources.models import SourceChunk
from projectmind.storage import ChunkRecord, DocumentRecord, Store

log = get_logger(__name__)

CONFIG_FILE = "browser.yaml"
ROOT_KEY = "browser"
CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)

#: A search counts as "leading somewhere" if an allowed page follows this soon.
SEARCH_WINDOW = timedelta(minutes=12)
MAX_QUERIES_PER_PAGE = 4

#: Documentation, reference and code-hosting sites. Deliberately technical; add
#: your own in `browser.yaml`.
DEFAULT_ALLOW = (
    "docs.python.org", "pypi.org", "pytorch.org", "huggingface.co", "kaggle.com",
    "arxiv.org", "paperswithcode.com", "github.com", "stackoverflow.com",
    "*.stackexchange.com", "*.readthedocs.io", "developer.mozilla.org", "docs.pydantic.dev",
    "fastapi.tiangolo.com", "numpy.org", "pandas.pydata.org", "scikit-learn.org",
    "docs.sqlalchemy.org", "postgresql.org", "sqlite.org", "supabase.com", "flutter.dev",
    "dart.dev", "pub.dev", "docs.docker.com", "learn.microsoft.com", "docs.anthropic.com",
    "platform.openai.com", "langchain.com", "python.langchain.com", "langchain-ai.github.io",
    "nodejs.org", "typescriptlang.org", "react.dev", "nextjs.org", "tailwindcss.com",
    "docs.github.com", "git-scm.com", "kubernetes.io", "wandb.ai", "lightgbm.readthedocs.io",
    "xgboost.readthedocs.io", "docs.ultralytics.com", "opencv.org", "docs.opencv.org",
)  # fmt: skip


@dataclass(slots=True)
class BrowserConfig:
    enabled: bool = False
    allow: tuple[str, ...] = DEFAULT_ALLOW
    deny: tuple[str, ...] = ()
    days: int = 365

    @classmethod
    def load(cls, home: Path) -> BrowserConfig:
        path = home / CONFIG_FILE
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        allow = tuple(str(item).lower() for item in raw.get("allow", DEFAULT_ALLOW))
        extra = tuple(str(item).lower() for item in raw.get("extra_allow", ()))
        return cls(
            enabled=bool(raw.get("enabled", True)),
            allow=allow + extra,
            deny=tuple(str(item).lower() for item in raw.get("deny", ())),
            days=int(raw.get("days", 365)),
        )

    def save(self, home: Path) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        path = home / CONFIG_FILE
        payload = {
            "enabled": self.enabled,
            "days": self.days,
            "allow": list(self.allow),
            "deny": list(self.deny),
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def permits(self, host: str) -> bool:
        host = host.lower().removeprefix("www.")
        if any(_host_matches(host, pattern) for pattern in self.deny):
            return False
        return any(_host_matches(host, pattern) for pattern in self.allow)


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.removeprefix("www.")
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == pattern or host.endswith("." + pattern)


@dataclass(frozen=True, slots=True)
class HistoryDatabase:
    browser: str
    profile: str
    path: Path


def find_databases() -> list[HistoryDatabase]:
    """Edge and Chrome history files for every profile on this machine."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    found: list[HistoryDatabase] = []
    layouts = (("edge", "Microsoft/Edge/User Data"), ("chrome", "Google/Chrome/User Data"))
    for browser, relative in layouts:
        root = Path(local) / relative
        if not root.is_dir():
            continue
        for profile in sorted(root.iterdir()):
            if profile.is_dir() and (
                profile.name == "Default" or profile.name.startswith("Profile ")
            ):
                history = profile / "History"
                if history.is_file():
                    found.append(HistoryDatabase(browser, profile.name, history))
    return found


def _from_chromium(microseconds: int) -> datetime:
    return CHROMIUM_EPOCH + timedelta(microseconds=microseconds)


def _to_chromium(moment: datetime) -> int:
    return int((moment - CHROMIUM_EPOCH).total_seconds() * 1_000_000)


@dataclass(slots=True)
class Page:
    url: str
    title: str
    visits: int
    first: datetime
    last: datetime
    queries: list[str] = field(default_factory=list)


def clean_url(url: str) -> str | None:
    """The page without query or fragment, or None if it is not a web page."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.hostname}{parts.path}".rstrip("/") or None


def _copy_database(source: Path) -> tuple[Path, Path]:
    """A private copy (with its journal, if any) so the live file is never touched."""
    directory = Path(tempfile.mkdtemp(prefix="pm-history-"))
    target = directory / "History"
    shutil.copy2(source, target)
    for suffix in ("-wal", "-journal", "-shm"):
        companion = source.with_name(source.name + suffix)
        if companion.is_file():
            shutil.copy2(companion, directory / ("History" + suffix))
    return directory, target


def read_pages(database: HistoryDatabase, config: BrowserConfig, *, now: datetime) -> list[Page]:
    """Allowed pages visited within the window, with the searches that led to them."""
    since = now - timedelta(days=config.days)
    directory, copy = _copy_database(database.path)
    try:
        connection = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return _read(connection, config, since)
        finally:
            connection.close()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _read(connection: sqlite3.Connection, config: BrowserConfig, since: datetime) -> list[Page]:
    floor = _to_chromium(since)
    rows = connection.execute(
        """
        SELECT u.id, u.url, u.title, v.visit_time
        FROM visits v JOIN urls u ON u.id = v.url
        WHERE v.visit_time >= ?
        ORDER BY v.visit_time
        """,
        (floor,),
    ).fetchall()

    pages: dict[str, Page] = {}
    allowed_visits: list[tuple[datetime, str]] = []
    search_visits: list[tuple[int, datetime]] = []
    search_url_ids = {
        row["url_id"]: row["term"]
        for row in connection.execute("SELECT url_id, term FROM keyword_search_terms")
    }

    for row in rows:
        moment = _from_chromium(row["visit_time"])
        if row["id"] in search_url_ids:
            search_visits.append((row["id"], moment))
            continue
        cleaned = clean_url(row["url"])
        if cleaned is None:
            continue
        host = urlsplit(cleaned).hostname or ""
        if not config.permits(host):
            continue
        page = pages.get(cleaned)
        if page is None:
            pages[cleaned] = Page(cleaned, (row["title"] or "").strip(), 1, moment, moment)
        else:
            page.visits += 1
            page.last = moment
            if row["title"]:
                page.title = row["title"].strip()
        allowed_visits.append((moment, cleaned))

    # A search is knowledge only if it led somewhere allowed.
    for url_id, moment in search_visits:
        term = search_url_ids[url_id].strip()
        if not term:
            continue
        for visited, cleaned in allowed_visits:
            if moment <= visited <= moment + SEARCH_WINDOW:
                queries = pages[cleaned].queries
                if term not in queries and len(queries) < MAX_QUERIES_PER_PAGE:
                    queries.append(term)
                break
    return list(pages.values())


def page_chunk(page: Page) -> SourceChunk:
    title = page.title or page.url
    span = (
        page.last.strftime("%Y-%m-%d")
        if page.first.date() == page.last.date()
        else f"{page.first:%Y-%m-%d} to {page.last:%Y-%m-%d}"
    )
    times = "once" if page.visits == 1 else f"{page.visits} times"
    lines = [title, page.url, f"Visited {times} ({span})."]
    if page.queries:
        lines.append("Found by searching: " + "; ".join(page.queries))
    return SourceChunk(
        text="\n".join(lines),
        heading=title,
        locator=f"visited {span}",
    )


@dataclass(slots=True)
class BrowserReport:
    databases: int = 0
    pages: int = 0
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    with_queries: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.indexed} pages indexed ({self.unchanged} unchanged, {self.removed} removed) "
            f"from {self.databases} browser profile(s)"
        )


def index_history(
    store: Store,
    config: BrowserConfig,
    *,
    databases: Iterable[HistoryDatabase] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> BrowserReport:
    """Read allowed history from every profile into the knowledge base."""
    moment = now or datetime.now(UTC)
    report = BrowserReport()
    merged: dict[str, Page] = {}
    for database in databases if databases is not None else find_databases():
        report.databases += 1
        try:
            pages = read_pages(database, config, now=moment)
        except (OSError, sqlite3.Error) as exc:
            report.errors.append(f"{database.browser}/{database.profile}: {exc}")
            continue
        for page in pages:
            existing = merged.get(page.url)
            if existing is None:
                merged[page.url] = page
                continue
            existing.visits += page.visits
            existing.first = min(existing.first, page.first)
            existing.last = max(existing.last, page.last)
            existing.title = existing.title or page.title
            for query in page.queries:
                if query not in existing.queries and len(existing.queries) < MAX_QUERIES_PER_PAGE:
                    existing.queries.append(query)

    report.pages = len(merged)
    report.with_queries = sum(1 for page in merged.values() if page.queries)
    if dry_run:
        return report

    known = store.document_hashes(ROOT_KEY)
    for page in merged.values():
        fingerprint = f"{page.visits}-{page.last.isoformat()}-{len(page.queries)}"
        if known.get(page.url) == fingerprint:
            report.unchanged += 1
            continue
        document = DocumentRecord(
            id=uuid4(),
            uri=page.url,
            source_type="web",
            root=ROOT_KEY,
            project_key="web",
            title=page.title or page.url,
            content_hash=fingerprint,
            mtime=page.last,
            indexed_at=moment,
        )
        chunk = page_chunk(page)
        store.replace_document(
            document,
            [ChunkRecord(id=uuid4(), document_id=document.id, ordinal=0, text=chunk.text,
                         heading=chunk.heading, locator=chunk.locator)],
        )  # fmt: skip
        report.indexed += 1

    # Pages that are no longer allowed or no longer in the window leave the index.
    for uri in known.keys() - merged.keys():
        if store.delete_document(uri):
            report.removed += 1
    log.info("browser history indexed", extra={"summary": report.summary()})
    return report
