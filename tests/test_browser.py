from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app
from projectmind.config import reset_settings
from projectmind.sources.browser import (
    BrowserConfig,
    HistoryDatabase,
    clean_url,
    index_history,
    page_chunk,
    read_pages,
)
from projectmind.storage import SqliteStore, Store

runner = CliRunner()
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


def chromium_time(moment: datetime) -> int:
    return int((moment - EPOCH).total_seconds() * 1_000_000)


class FakeBrowser:
    """A Chromium-format History database, built the way the real one is."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.executescript(
            """
            CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,
                               visit_count INTEGER, last_visit_time INTEGER);
            CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);
            CREATE TABLE keyword_search_terms (keyword_id INTEGER, url_id INTEGER,
                                               term TEXT, normalized_term TEXT);
            """
        )
        self._next = 1

    def visit(self, url: str, title: str, at: datetime) -> int:
        existing = self.connection.execute("SELECT id FROM urls WHERE url = ?", (url,)).fetchone()
        url_id = existing[0] if existing else self._next
        if not existing:
            self.connection.execute(
                "INSERT INTO urls (id, url, title, visit_count, last_visit_time) "
                "VALUES (?,?,?,?,?)",
                (url_id, url, title, 1, chromium_time(at)),
            )
            self._next += 1
        self.connection.execute(
            "INSERT INTO visits (url, visit_time) VALUES (?, ?)", (url_id, chromium_time(at))
        )
        return int(url_id)

    def search(self, term: str, at: datetime) -> None:
        url_id = self.visit(
            f"https://www.google.com/search?q={term.replace(' ', '+')}", f"{term} - Google", at
        )
        self.connection.execute(
            "INSERT INTO keyword_search_terms VALUES (1, ?, ?, ?)", (url_id, term, term.lower())
        )

    def done(self) -> HistoryDatabase:
        self.connection.commit()
        self.connection.close()
        return HistoryDatabase("edge", "Default", self.path)


@pytest.fixture
def browser(tmp_path: Path) -> FakeBrowser:
    return FakeBrowser(tmp_path / "History")


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def config(**overrides: object) -> BrowserConfig:
    return BrowserConfig(enabled=True, **overrides)  # type: ignore[arg-type]


class TestUrls:
    def test_query_and_fragment_are_stripped(self) -> None:
        assert (
            clean_url("https://docs.python.org/3/library/re.html?token=abc#section")
            == "https://docs.python.org/3/library/re.html"
        )

    def test_only_web_pages_count(self) -> None:
        assert clean_url("file:///C:/secret.txt") is None
        assert clean_url("chrome://settings") is None
        assert clean_url("not a url") is None


class TestAllowList:
    def test_defaults_permit_documentation_and_nothing_else(self) -> None:
        cfg = config()
        assert cfg.permits("docs.python.org")
        assert cfg.permits("www.github.com")
        assert cfg.permits("fastapi.readthedocs.io")
        assert not cfg.permits("mail.google.com")
        assert not cfg.permits("netbanking.example.com")
        assert not cfg.permits("www.facebook.com")

    def test_a_subdomain_of_an_allowed_domain_is_allowed(self) -> None:
        assert config().permits("gist.github.com")

    def test_deny_wins_over_allow(self) -> None:
        cfg = config(deny=("github.com",))
        assert not cfg.permits("github.com")

    def test_a_lookalike_domain_is_not_allowed(self) -> None:
        assert not config().permits("github.com.evil.example")
        assert not config().permits("notgithub.com")


class TestReading:
    def test_only_allowed_pages_are_read(self, browser: FakeBrowser) -> None:
        browser.visit(
            "https://docs.python.org/3/library/re.html", "re - Python docs", NOW - timedelta(days=2)
        )
        browser.visit("https://mail.google.com/mail/u/0/", "Inbox", NOW - timedelta(days=1))
        browser.visit("https://bank.example.com/account", "My account", NOW - timedelta(days=1))
        pages = read_pages(browser.done(), config(), now=NOW)
        assert [page.url for page in pages] == ["https://docs.python.org/3/library/re.html"]

    def test_visits_are_counted_and_dated(self, browser: FakeBrowser) -> None:
        for days in (10, 5, 1):
            browser.visit(
                "https://docs.python.org/3/library/re.html?x=1", "re", NOW - timedelta(days=days)
            )
        [page] = read_pages(browser.done(), config(), now=NOW)
        assert page.visits == 3
        assert page.first < page.last

    def test_the_window_is_respected(self, browser: FakeBrowser) -> None:
        browser.visit("https://docs.python.org/old", "old", NOW - timedelta(days=900))
        browser.visit("https://docs.python.org/new", "new", NOW - timedelta(days=3))
        pages = read_pages(browser.done(), config(days=365), now=NOW)
        assert [page.url for page in pages] == ["https://docs.python.org/new"]

    def test_a_search_that_led_to_an_allowed_page_is_kept_with_that_page(
        self, browser: FakeBrowser
    ) -> None:
        at = NOW - timedelta(days=2)
        browser.search("pgvector hnsw index tuning", at)
        browser.visit(
            "https://www.postgresql.org/docs/current/indexes.html",
            "Indexes",
            at + timedelta(minutes=2),
        )
        [page] = read_pages(browser.done(), config(), now=NOW)
        assert page.queries == ["pgvector hnsw index tuning"]

    def test_a_search_that_led_nowhere_allowed_is_dropped_entirely(
        self, browser: FakeBrowser
    ) -> None:
        at = NOW - timedelta(days=2)
        browser.search("private personal matter", at)
        browser.visit("https://shop.example.com/item", "A shop", at + timedelta(minutes=1))
        browser.visit("https://docs.python.org/3/library/re.html", "re", at + timedelta(days=1))
        [page] = read_pages(browser.done(), config(), now=NOW)
        assert page.queries == []

    def test_a_search_too_long_before_the_page_does_not_count(self, browser: FakeBrowser) -> None:
        at = NOW - timedelta(days=2)
        browser.search("unrelated earlier search", at)
        browser.visit("https://docs.python.org/3/library/re.html", "re", at + timedelta(hours=3))
        [page] = read_pages(browser.done(), config(), now=NOW)
        assert page.queries == []

    def test_the_live_database_is_not_modified(self, browser: FakeBrowser) -> None:
        browser.visit("https://docs.python.org/3/library/re.html", "re", NOW - timedelta(days=1))
        database = browser.done()
        before = database.path.stat().st_mtime_ns, database.path.read_bytes()
        read_pages(database, config(), now=NOW)
        assert (database.path.stat().st_mtime_ns, database.path.read_bytes()) == before


class TestIndexing:
    def make(self, tmp_path: Path) -> HistoryDatabase:
        browser = FakeBrowser(tmp_path / "H")
        at = NOW - timedelta(days=2)
        browser.search("sqlite fts5 bm25 ranking", at)
        browser.visit(
            "https://sqlite.org/fts5.html?q=1", "SQLite FTS5 Extension", at + timedelta(minutes=1)
        )
        browser.visit("https://mail.google.com/", "Inbox", at)
        return browser.done()

    def test_a_page_becomes_a_cited_searchable_document(self, store: Store, tmp_path: Path) -> None:
        report = index_history(store, config(), databases=[self.make(tmp_path)], now=NOW)
        assert report.indexed == 1
        [hit] = store.search_chunks(["fts5", "bm25"])
        assert hit.document.source_type == "web"
        assert hit.document.uri == "https://sqlite.org/fts5.html"
        assert "Found by searching: sqlite fts5 bm25 ranking" in hit.chunk.text
        assert hit.chunk.locator.startswith("visited")

    def test_nothing_from_a_disallowed_site_is_stored(self, store: Store, tmp_path: Path) -> None:
        index_history(store, config(), databases=[self.make(tmp_path)], now=NOW)
        assert store.search_chunks(["inbox"]) == []
        assert all("mail.google" not in doc.uri for doc in store.list_documents())

    def test_a_second_pass_changes_nothing(self, store: Store, tmp_path: Path) -> None:
        database = self.make(tmp_path)
        index_history(store, config(), databases=[database], now=NOW)
        again = index_history(store, config(), databases=[database], now=NOW)
        assert again.indexed == 0
        assert again.unchanged == 1

    def test_a_domain_that_is_denied_later_leaves_the_index(
        self, store: Store, tmp_path: Path
    ) -> None:
        database = self.make(tmp_path)
        index_history(store, config(), databases=[database], now=NOW)
        later = index_history(store, config(deny=("sqlite.org",)), databases=[database], now=NOW)
        assert later.removed == 1
        assert store.search_chunks(["fts5"]) == []

    def test_a_dry_run_stores_nothing(self, store: Store, tmp_path: Path) -> None:
        report = index_history(
            store, config(), databases=[self.make(tmp_path)], now=NOW, dry_run=True
        )
        assert report.pages == 1
        assert report.with_queries == 1
        assert store.count_documents() == 0

    def test_two_browsers_visiting_the_same_page_are_merged(
        self, store: Store, tmp_path: Path
    ) -> None:
        first = FakeBrowser(tmp_path / "A")
        first.visit("https://docs.python.org/3/library/re.html", "re", NOW - timedelta(days=4))
        second = FakeBrowser(tmp_path / "B")
        second.visit("https://docs.python.org/3/library/re.html", "re", NOW - timedelta(days=1))
        report = index_history(store, config(), databases=[first.done(), second.done()], now=NOW)
        assert report.pages == 1
        assert report.databases == 2
        [hit] = store.search_chunks(["python", "docs"])
        assert "Visited 2 times" in hit.chunk.text

    def test_an_unreadable_database_is_reported_not_fatal(
        self, store: Store, tmp_path: Path
    ) -> None:
        broken = tmp_path / "History"
        broken.write_bytes(b"this is not a sqlite database at all" * 10)
        report = index_history(
            store, config(), databases=[HistoryDatabase("chrome", "Default", broken)], now=NOW
        )
        assert report.errors
        assert report.indexed == 0

    def test_the_chunk_says_what_it_is(self) -> None:
        from projectmind.sources.browser import Page

        page = Page("https://x.example/a", "A page", 1, NOW, NOW, ["why is x"])
        text = page_chunk(page).text
        assert "Visited once" in text
        assert "https://x.example/a" in text
        assert "Found by searching: why is x" in text


class TestCommands:
    @pytest.fixture(autouse=True)
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        path = tmp_path / "pmhome"
        monkeypatch.setenv("PROJECTMIND_HOME", str(path))
        monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))
        reset_settings()
        runner.invoke(app, ["init", "--no-seed"])
        yield path
        reset_settings()

    def test_it_is_off_by_default(self) -> None:
        assert BrowserConfig.load(Path("does-not-exist")).enabled is False
        result = runner.invoke(app, ["browser", "scan"])
        assert result.exit_code == 1

    def test_enable_allow_deny_roundtrip(self, home: Path) -> None:
        assert runner.invoke(app, ["browser", "enable", "--days", "30"]).exit_code == 0
        assert runner.invoke(app, ["browser", "allow", "docs.example.com"]).exit_code == 0
        assert runner.invoke(app, ["browser", "deny", "github.com"]).exit_code == 0
        loaded = BrowserConfig.load(home)
        assert loaded.enabled
        assert loaded.days == 30
        assert loaded.permits("docs.example.com")
        assert not loaded.permits("github.com")

    def test_a_dry_run_works_while_off_and_with_no_browser(self) -> None:
        result = runner.invoke(app, ["browser", "scan", "--dry-run"])
        assert result.exit_code == 0
        assert "would store 0 pages" in result.stdout

    def test_disable_forgets_what_was_indexed(self, home: Path) -> None:
        runner.invoke(app, ["browser", "enable"])
        result = runner.invoke(app, ["browser", "disable"])
        assert result.exit_code == 0
        assert not BrowserConfig.load(home).enabled
