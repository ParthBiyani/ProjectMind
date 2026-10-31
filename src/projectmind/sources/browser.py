"""Browser history from Edge and Chrome, minus the sites you never want read.

Browser history is the most sensitive data on a developer's machine. The rule is
"everything you read, except what you have ruled out", with the ruling-out done
first and done in three places (the extension, the daemon and this reader):

* **A never-capture list.** Banking, brokers, payments, mail, chat, social media,
  sign-in pages, password managers, government identity and tax sites, health,
  personal documents, cloud consoles and AI chats are on it from the start. Add
  and remove sites in the extension's options or with `projectmind browser deny`.
* **Never this machine.** localhost, private network addresses and `.local` names
  are never read, whatever the list says: that is where dashboards (including
  ProjectMind's own) and development servers live.
* **Nothing but the page.** URLs are stored without query string or fragment (no
  tokens, no session ids), with the page title and a visit count.
* **Searches only with a purpose.** A search query is kept only if, within a few
  minutes, you went on to a page that may be read.
* **A copy, read-only.** The browser's own database is copied and the copy is
  read. The original is never opened for writing.

Each stored item says what it is: `Visited <title> N times (first..last)` and,
when applicable, `Found by searching: ...`.
"""

from __future__ import annotations

import ipaddress
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
CONFIG_VERSION = 2
ROOT_KEY = "browser"
CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)

#: A search counts as "leading somewhere" if a readable page follows this soon.
SEARCH_WINDOW = timedelta(minutes=12)
MAX_QUERIES_PER_PAGE = 4

#: Never read, from the first run. A domain covers its subdomains. Edit the list
#: in the extension's options, `projectmind browser deny / allow`, or browser.yaml.
DEFAULT_DENY = (
    # brokers, trading and investing
    "zerodha.com", "kite.com", "fyers.in", "fyers.com", "groww.in", "upstox.com",
    "angelone.in", "dhan.co", "5paisa.com", "icicidirect.com", "hdfcsec.com",
    "motilaloswal.com", "sharekhan.com", "smallcase.com", "paytmmoney.com", "indmoney.com",
    "coinbase.com", "binance.com", "wazirx.com", "coindcx.com",
    # banks, cards and payments
    "hdfcbank.com", "icicibank.com", "onlinesbi.sbi", "sbi.co.in", "sbicard.com",
    "axisbank.com", "kotak.com", "yesbank.in", "pnbindia.in", "bankofbaroda.in",
    "idfcfirstbank.com", "indusind.com", "federalbank.co.in", "aubank.in",
    "americanexpress.com", "paytm.com", "phonepe.com", "cred.club", "razorpay.com",
    "paypal.com", "stripe.com", "wise.com", "policybazaar.com",
    # government identity and tax
    "gov.in", "nic.in", "uidai.gov.in", "incometax.gov.in", "digilocker.gov.in",
    "passportindia.gov.in", "irs.gov",
    # mail, chat and social
    "mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com",
    "mail.yahoo.com", "proton.me", "whatsapp.com", "web.telegram.org", "discord.com",
    "slack.com", "messenger.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "linkedin.com", "snapchat.com",
    # sign-in, accounts and passwords
    "accounts.google.com", "myaccount.google.com", "login.microsoftonline.com",
    "login.live.com", "account.live.com", "account.microsoft.com", "appleid.apple.com",
    "1password.com", "bitwarden.com", "lastpass.com",
    # personal documents and storage
    "docs.google.com", "drive.google.com", "photos.google.com", "onedrive.live.com",
    "dropbox.com", "icloud.com",
    # cloud consoles (keys and bills)
    "console.aws.amazon.com", "portal.azure.com", "console.cloud.google.com",
    "dashboard.render.com", "vercel.com",
    # AI chats (private conversations)
    "claude.ai", "chatgpt.com", "chat.openai.com", "gemini.google.com",
    # health
    "practo.com", "1mg.com", "pharmeasy.in", "apollo247.com",
    # shopping and orders
    "amazon.in", "amazon.com", "flipkart.com", "myntra.com", "swiggy.com", "zomato.com",
    "blinkit.com", "zeptonow.com", "bigbasket.com",
)  # fmt: skip

#: Never read, and not on the list because it cannot be taken off it.
LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


def is_local(host: str) -> bool:
    """This machine, the local network, or a name that only resolves inside it."""
    host = host.lower().strip("[]").rstrip(".")
    if not host or host == "localhost" or ("." not in host and ":" not in host):
        return True
    if host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def clean_domain(domain: str) -> str | None:
    """`https://www.Kite.com/login` -> `kite.com`. None if it is not a domain."""
    text = domain.strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    text = text.split("/", 1)[0].removeprefix("*.").removeprefix("www.").strip(".")
    if not text or " " in text or "." not in text:
        return None
    return text


@dataclass(slots=True)
class BrowserConfig:
    enabled: bool = False
    deny: tuple[str, ...] = DEFAULT_DENY
    days: int = 365

    @classmethod
    def load(cls, home: Path) -> BrowserConfig:
        path = home / CONFIG_FILE
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        listed = tuple(str(item).lower() for item in raw.get("deny") or ())
        if int(raw.get("version", 1)) < CONFIG_VERSION:
            # Version 1 read only an allow-list; its deny list was extra. Moving to
            # "everything except", the defaults join whatever was denied before.
            listed = tuple(dict.fromkeys((*DEFAULT_DENY, *listed)))
        return cls(
            enabled=bool(raw.get("enabled", True)),
            deny=listed,
            days=int(raw.get("days", 365)),
        )

    def save(self, home: Path) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        path = home / CONFIG_FILE
        payload = {
            "version": CONFIG_VERSION,
            "enabled": self.enabled,
            "days": self.days,
            "deny": list(self.deny),
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def permits(self, host: str) -> bool:
        host = host.lower().removeprefix("www.")
        if is_local(host):
            return False
        return not any(_host_matches(host, pattern) for pattern in self.deny)

    def denying(self, domain: str) -> BrowserConfig:
        """This config with `domain` on the never-capture list."""
        if domain in self.deny:
            return self
        return BrowserConfig(self.enabled, (*self.deny, domain), self.days)

    def allowing(self, domain: str) -> BrowserConfig:
        """This config with `domain` taken off the never-capture list."""
        return BrowserConfig(self.enabled, tuple(d for d in self.deny if d != domain), self.days)


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
