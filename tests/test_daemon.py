from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind import hook_entry
from projectmind.config import Settings
from projectmind.daemon import Daemon, load_token
from projectmind.evaluation.fixtures import FixtureCorpus
from projectmind.evaluation.live import load_corpus
from projectmind.sources.config import RootConfig, SourcesConfig
from projectmind.storage import HistoryFilter, SqliteStore

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = "How does the ADX block return its values?"


def write_notes(root: Path) -> None:
    notes = root / "alpha" / "NOTES.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        "# Alpha\n\n## Indicators\n\nThe ADX block returns di plus, di minus and adx as a tuple.\n",
        encoding="utf-8",
    )
    stamp = notes.stat().st_mtime - 7_200
    os.utime(notes, (stamp, stamp))


@pytest.fixture
def running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Daemon]:
    home = tmp_path / "pm"
    home.mkdir()
    monkeypatch.setenv("PROJECTMIND_HOME", str(home))
    monkeypatch.delenv("PROJECTMIND_NO_DAEMON", raising=False)
    root = tmp_path / "Projects"
    write_notes(root)
    SourcesConfig(roots=[RootConfig(path=root, depth=1)]).save(home)
    settings = Settings(home=home, db_url=None, recency_half_life_days=1e6)
    store = SqliteStore(home / "projectmind.db")
    store.migrate()
    load_corpus(store, FixtureCorpus.load(REPO_ROOT / "eval" / "fixtures" / "corpus.yaml"))
    from projectmind.sources.indexer import KnowledgeIndexer

    KnowledgeIndexer(store).scan_root(SourcesConfig.load(home).roots[0])
    store.close()
    daemon = Daemon(settings, port=0, sync_minutes=0)
    daemon.start()
    yield daemon
    daemon.stop()


def call(
    daemon: Daemon,
    method: str,
    path: str,
    body: object | None = None,
    *,
    token: str | None = None,
    host: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not False:  # type: ignore[comparison-overlap]
        headers["Authorization"] = f"Bearer {token or daemon.token}"
    if host:
        headers["Host"] = host
    connection.request(
        method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    data = response.read()
    result = (response.status, data, {k: v for k, v in response.getheaders()})
    connection.close()
    return result


class TestAnnouncement:
    def test_the_daemon_announces_where_it_listens(self, running: Daemon) -> None:
        info = hook_entry.daemon_info(running.home)
        assert info is not None
        assert info["port"] == running.port
        assert info["token"] == running.token

    def test_the_token_is_stable_across_restarts(self, tmp_path: Path) -> None:
        assert load_token(tmp_path) == load_token(tmp_path)
        assert len(load_token(tmp_path)) >= 32

    def test_stopping_removes_the_announcement(self, running: Daemon) -> None:
        running.stop()
        assert hook_entry.daemon_info(running.home) is None
        running.stop = lambda: None  # type: ignore[method-assign]  # fixture calls it again


class TestSecurity:
    def test_a_request_without_the_token_is_refused(self, running: Daemon) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        connection.request("POST", "/context", body=b"{}")
        assert connection.getresponse().status == 401

    def test_a_wrong_token_is_refused(self, running: Daemon) -> None:
        status, _, _ = call(running, "POST", "/context", {}, token="x" * 40)
        assert status == 401

    def test_a_foreign_host_header_is_refused_even_with_the_token(self, running: Daemon) -> None:
        """Stops DNS rebinding: a web page cannot reach the daemon under its own name."""
        status, _, _ = call(running, "POST", "/context", {}, host="evil.example")
        assert status == 403

    def test_no_cors_headers_are_ever_sent(self, running: Daemon) -> None:
        _, _, headers = call(running, "GET", "/status")
        assert not any(key.lower().startswith("access-control") for key in headers)

    def test_only_loopback_is_bound(self, running: Daemon) -> None:
        assert running.server.server_address[0] == "127.0.0.1"

    def test_an_oversized_body_is_refused(self, running: Daemon) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        connection.putrequest("POST", "/context")
        connection.putheader("Authorization", f"Bearer {running.token}")
        connection.putheader("Content-Length", str(50_000_000))
        connection.endheaders()
        assert connection.getresponse().status == 413

    def test_unknown_routes_are_not_found(self, running: Daemon) -> None:
        status, _, _ = call(running, "POST", "/nope", {})
        assert status == 404


class TestContext:
    def test_a_prompt_gets_cited_context_and_a_bundle_id(self, running: Daemon) -> None:
        status, body, headers = call(
            running, "POST", "/context", {"prompt": PROMPT, "cwd": "alpha", "session_id": "s-1"}
        )
        assert status == 200
        body.decode("ascii")
        context = json.loads(body)["hookSpecificOutput"]["additionalContext"]
        assert "NOTES.md:" in context
        assert headers[hook_entry.BUNDLE_HEADER]

    def test_a_trivial_prompt_is_a_204(self, running: Daemon) -> None:
        status, body, _ = call(running, "POST", "/context", {"prompt": "rename this variable"})
        assert status == 204
        assert body == b""

    def test_the_interaction_is_logged_with_its_source_and_session(self, running: Daemon) -> None:
        call(running, "POST", "/context", {"prompt": PROMPT, "cwd": "alpha", "session_id": "s-9"})
        store = SqliteStore(running.home / "projectmind.db")
        try:
            [entry] = store.find_bundles(HistoryFilter(source="hook"))
        finally:
            store.close()
        assert entry.session_id == "s-9"

    def test_delivery_can_be_confirmed(self, running: Daemon) -> None:
        _, _, headers = call(running, "POST", "/context", {"prompt": PROMPT, "cwd": "alpha"})
        bundle_id = headers[hook_entry.BUNDLE_HEADER]
        status, _, _ = call(
            running, "POST", "/delivered", {"bundle_id": bundle_id, "delivered": True}
        )
        assert status == 200
        store = SqliteStore(running.home / "projectmind.db")
        try:
            entries = store.find_bundles(HistoryFilter())
        finally:
            store.close()
        assert entries[0].delivered is True

    def test_a_bad_bundle_id_is_a_400(self, running: Daemon) -> None:
        status, _, _ = call(running, "POST", "/delivered", {"bundle_id": "not-a-uuid"})
        assert status == 400

    def test_concurrent_requests_all_get_answers(self, running: Daemon) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def one(_: int) -> int:
            return call(running, "POST", "/context", {"prompt": PROMPT, "cwd": "alpha"})[0]

        with ThreadPoolExecutor(max_workers=6) as pool:
            assert set(pool.map(one, range(12))) == {200}


class TestStatusAndSync:
    def test_status_reports_what_is_indexed(self, running: Daemon) -> None:
        status, body, _ = call(running, "GET", "/status")
        data = json.loads(body)
        assert status == 200
        assert data["chunks"] >= 1
        assert data["records"] >= 1

    def test_health_needs_no_token_but_still_checks_the_host(self, running: Daemon) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        connection.request("GET", "/health")
        assert connection.getresponse().status == 200
        status, _, _ = call(running, "GET", "/health", token=None, host="evil.example")
        assert status == 403

    def test_a_sync_can_be_requested_and_picks_up_a_new_file(self, running: Daemon) -> None:
        root = SourcesConfig.load(running.home).roots[0].path
        fresh = root / "beta" / "NOTES.md"
        fresh.parent.mkdir(parents=True)
        fresh.write_text(
            "# Beta\n\nThe zebrafish pipeline uses light sheet imaging.\n", encoding="utf-8"
        )
        stamp = fresh.stat().st_mtime - 7_200
        os.utime(fresh, (stamp, stamp))
        status, _, _ = call(running, "POST", "/sync", {})
        assert status == 202
        deadline = time.time() + 30
        while time.time() < deadline and (running.sync_running.is_set() or not running.last_sync):
            time.sleep(0.1)
        _, body, _ = call(running, "POST", "/context", {"prompt": "zebrafish light sheet pipeline"})
        assert b"zebrafish" in body

    def test_shutdown_stops_the_daemon(self, running: Daemon) -> None:
        status, _, _ = call(running, "POST", "/shutdown", {})
        assert status == 200
        assert running.stop_event.wait(10)
        running.stop = lambda: None  # type: ignore[method-assign]


class TestHookClient:
    def run_hook(
        self, running: Daemon, payload: dict[str, str], **env: str
    ) -> subprocess.CompletedProcess[bytes]:
        merged = {k: v for k, v in os.environ.items() if k != "PROJECTMIND_NO_DAEMON"}
        merged.update(PROJECTMIND_HOME=str(running.home), **env)
        return subprocess.run(
            [sys.executable, "-I", "-m", "projectmind.hook_entry", "user-prompt-submit"],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=merged,
            timeout=60,
            check=False,
        )

    def test_the_hook_is_answered_by_the_daemon(self, running: Daemon) -> None:
        result = self.run_hook(
            running, {"prompt": PROMPT, "cwd": "alpha", "session_id": "via-daemon"}
        )
        assert result.returncode == 0
        assert result.stderr == b""
        assert "NOTES.md:" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        store = SqliteStore(running.home / "projectmind.db")
        try:
            [entry] = store.find_bundles(HistoryFilter(source="hook"))
        finally:
            store.close()
        assert entry.session_id == "via-daemon"
        assert entry.delivered is True

    def test_the_daemon_path_does_not_import_the_heavy_modules(self, running: Daemon) -> None:
        probe = (
            "import sys, runpy\n"
            "sys.argv = ['hook_entry', 'user-prompt-submit']\n"
            "import io; sys.stdin = io.TextIOWrapper(io.BytesIO(b'{}'))\n"
            "import projectmind.hook_entry as h\n"
            "bad = [m for m in ('pydantic', 'pydantic_settings', 'projectmind.models', "
            "'projectmind.config', 'projectmind.service') if m in sys.modules]\n"
            "print(bad)\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=False
        )
        assert result.stdout.strip() == "[]", result.stdout + result.stderr

    def test_without_a_daemon_the_hook_still_answers_in_process(self, running: Daemon) -> None:
        result = self.run_hook(
            running, {"prompt": PROMPT, "cwd": "alpha"}, PROJECTMIND_NO_DAEMON="1"
        )
        assert result.returncode == 0
        assert "NOTES.md:" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_a_stale_announcement_falls_back_to_in_process(self, tmp_path: Path) -> None:
        home = tmp_path / "pm"
        home.mkdir()
        (home / hook_entry.DAEMON_FILE).write_text(
            json.dumps({"pid": 1, "port": 1, "token": "x" * 40}), encoding="utf-8"
        )
        env = {k: v for k, v in os.environ.items() if k != "PROJECTMIND_NO_DAEMON"}
        env["PROJECTMIND_HOME"] = str(home)
        result = subprocess.run(
            [sys.executable, "-I", "-m", "projectmind.hook_entry", "user-prompt-submit"],
            input=json.dumps({"prompt": "hello there", "cwd": str(tmp_path)}).encode(),
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0
        assert result.stderr == b""

    def test_a_bundle_id_is_a_uuid_or_nothing(self) -> None:
        assert hook_entry.confirm_with_daemon(uuid4(), True) in {True, False}
