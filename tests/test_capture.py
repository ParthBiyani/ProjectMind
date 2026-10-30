from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from projectmind.capture import (
    MAX_TEXT_CHARS,
    TABS_URI,
    handle_config,
    handle_page,
    handle_tabs,
)
from projectmind.config import Settings
from projectmind.daemon import Daemon
from projectmind.service import MemoryService
from projectmind.sources.browser import BrowserConfig
from projectmind.storage import SqliteStore

PAGE = (
    "Hybrid search in PostgreSQL combines full text ranking with vector similarity. "
    "A reciprocal rank fusion step merges the two result lists into one ordering. "
) * 3


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[MemoryService]:
    home = tmp_path / "pm"
    home.mkdir()
    BrowserConfig(enabled=True).save(home)
    store = SqliteStore(home / "projectmind.db")
    store.migrate()
    service = MemoryService(store, Settings(home=home, db_url=None))
    yield service
    service.close()


def page(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "url": "https://www.postgresql.org/docs/current/textsearch.html?utm=x#intro",
        "title": "Full Text Search",
        "text": PAGE,
    }
    event.update(overrides)
    return event


class TestPages:
    def test_an_allowed_page_is_stored_and_searchable(self, memory: MemoryService) -> None:
        result = handle_page(memory, page())
        assert result["stored"] is True
        [hit] = memory.store.search_chunks(["reciprocal", "rank", "fusion"], limit=1)
        assert hit.document.uri == "https://www.postgresql.org/docs/current/textsearch.html"
        assert hit.document.title == "Full Text Search"
        assert hit.chunk.locator.startswith("captured ")

    def test_query_strings_and_fragments_never_reach_the_store(self, memory: MemoryService) -> None:
        handle_page(memory, page())
        assert all(
            "utm" not in doc.uri and "#" not in doc.uri for doc in memory.store.list_documents()
        )

    def test_a_page_on_a_domain_that_is_not_allowed_is_refused(self, memory: MemoryService) -> None:
        result = handle_page(memory, page(url="https://bank.example.com/account"))
        assert result["stored"] is False
        assert "allow-list" in str(result["reason"])
        assert memory.store.count_documents() == 0

    def test_explicit_remember_overrides_the_allow_list(self, memory: MemoryService) -> None:
        result = handle_page(memory, page(url="https://blog.example.org/post", explicit=True))
        assert result["stored"] is True

    def test_the_deny_list_beats_even_an_explicit_request(self, tmp_path: Path) -> None:
        home = tmp_path / "pm2"
        home.mkdir()
        BrowserConfig(enabled=True, deny=("example.org",)).save(home)
        store = SqliteStore(":memory:")
        store.migrate()
        service = MemoryService(store, Settings(home=home, db_url=None))
        result = handle_page(service, page(url="https://blog.example.org/post", explicit=True))
        assert result["stored"] is False
        assert "denied" in str(result["reason"])

    def test_nothing_is_stored_while_capture_is_off(self, tmp_path: Path) -> None:
        home = tmp_path / "off"
        home.mkdir()
        store = SqliteStore(":memory:")
        store.migrate()
        service = MemoryService(store, Settings(home=home, db_url=None))
        result = handle_page(service, page())
        assert result["stored"] is False
        assert "off" in str(result["reason"])

    def test_a_chunk_that_looks_like_a_credential_is_dropped(self, memory: MemoryService) -> None:
        secret = "token: ghp_" + "aB3dE6gH9" * 4
        # The page body is longer than one chunk, so the secret sits in a chunk of its own.
        result = handle_page(
            memory,
            page(text=f"Setup notes for the gateway. {secret} keep it.\n\n" + PAGE * 8),
        )
        assert result["stored"] is True
        assert result["withheld"] >= 1
        assert memory.store.search_chunks(["ghp"]) == []

    def test_text_is_cut_to_the_size_limit(self, memory: MemoryService) -> None:
        handle_page(memory, page(text=("word " * 40_000)))
        [doc] = memory.store.list_documents()
        total = sum(len(hit.chunk.text) for hit in memory.store.search_chunks(["word"], limit=500))
        assert doc.chunk_count >= 1
        assert total <= MAX_TEXT_CHARS + 500

    def test_a_page_with_no_real_text_is_refused(self, memory: MemoryService) -> None:
        assert handle_page(memory, page(text="Menu Home About"))["stored"] is False

    def test_a_non_web_url_is_refused(self, memory: MemoryService) -> None:
        assert handle_page(memory, page(url="file:///C:/secret.txt"))["stored"] is False
        assert handle_page(memory, page(url="chrome://settings"))["stored"] is False

    def test_an_unchanged_page_is_not_rewritten(self, memory: MemoryService) -> None:
        handle_page(memory, page())
        assert handle_page(memory, page())["reason"] == "unchanged"

    def test_a_changed_page_replaces_the_old_text(self, memory: MemoryService) -> None:
        handle_page(memory, page())
        handle_page(memory, page(text="Completely different content about zebrafish imaging " * 4))
        assert memory.store.search_chunks(["reciprocal"]) == []
        assert memory.store.search_chunks(["zebrafish"])


class TestTabs:
    def test_only_allowed_tabs_are_recorded(self, memory: MemoryService) -> None:
        result = handle_tabs(
            memory,
            {
                "tabs": [
                    {"url": "https://docs.python.org/3/library/re.html?x=1", "title": "re"},
                    {"url": "https://mail.google.com/mail/u/0/", "title": "Inbox"},
                ]
            },
        )
        assert result["tabs"] == 1
        [hit] = memory.store.search_chunks(["open", "browser"])
        assert hit.document.uri == TABS_URI
        assert "docs.python.org/3/library/re.html" in hit.chunk.text
        assert "Inbox" not in hit.chunk.text

    def test_a_new_snapshot_replaces_the_last_one(self, memory: MemoryService) -> None:
        handle_tabs(memory, {"tabs": [{"url": "https://docs.python.org/a", "title": "a"}]})
        handle_tabs(memory, {"tabs": [{"url": "https://pytorch.org/b", "title": "b"}]})
        assert memory.store.count_documents() == 1
        [hit] = memory.store.search_chunks(["browser"])
        assert "pytorch.org/b" in hit.chunk.text
        assert "docs.python.org/a" not in hit.chunk.text

    def test_closing_every_allowed_tab_clears_the_snapshot(self, memory: MemoryService) -> None:
        handle_tabs(memory, {"tabs": [{"url": "https://docs.python.org/a", "title": "a"}]})
        handle_tabs(memory, {"tabs": [{"url": "https://mail.google.com/", "title": "x"}]})
        assert memory.store.count_documents() == 0


class TestConfigEndpoint:
    def test_the_extension_can_ask_what_the_rules_are(self, memory: MemoryService) -> None:
        config = handle_config(memory, {})
        assert config["enabled"] is True
        assert "docs.python.org" in config["allow"]  # type: ignore[operator]


class TestOverHttp:
    @pytest.fixture
    def daemon(self, tmp_path: Path) -> Iterator[Daemon]:
        home = tmp_path / "pm"
        home.mkdir()
        BrowserConfig(enabled=True).save(home)
        daemon = Daemon(Settings(home=home, db_url=None), port=0, sync_minutes=0)
        daemon.start()
        yield daemon
        daemon.stop()

    def post(self, daemon: Daemon, path: str, body: object) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
        connection.request(
            "POST",
            path,
            body=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {daemon.token}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, data

    def test_an_extension_post_is_stored_and_then_served_back_with_a_citation(
        self, daemon: Daemon
    ) -> None:
        status, result = self.post(daemon, "/capture/page", page())
        assert (status, result["stored"]) == (200, True)
        status, _ = self.post(
            daemon, "/context", {"prompt": "How does reciprocal rank fusion merge the two lists?"}
        )
        assert status in {200, 204}
        with daemon.lock:
            hits = daemon.memory.store.search_chunks(["reciprocal", "fusion"])
        assert hits
        assert hits[0].document.uri.endswith("textsearch.html")

    def test_an_unknown_capture_kind_is_a_404(self, daemon: Daemon) -> None:
        status, _ = self.post(daemon, "/capture/keystrokes", {})
        assert status == 404

    def test_the_config_route_returns_the_rules(self, daemon: Daemon) -> None:
        status, result = self.post(daemon, "/capture/config", {})
        assert status == 200
        assert result["enabled"] is True
