"""The local daemon: a warm memory, a scheduler and a door for the browser.

Why a daemon at all: a hook that starts a Python process for every prompt pays
for interpreter start-up, imports and opening the database each time. A daemon
pays those once. The hook becomes a thin client that asks it over localhost.

What it does:

* answers `/context` for the hook, from a database that is already open;
* runs `sync` on a timer, at low priority, so the knowledge base keeps growing
  without anyone remembering to run anything;
* accepts captures from the browser extension at `/capture`.

Security model, because a local web server is reachable by every page you open:
it binds only to 127.0.0.1, every request must carry a bearer token that lives in
a file only your account can read, the `Host` header must be the loopback
address (which stops DNS-rebinding), and it sends no CORS headers, so a web page
cannot read an answer even if it could send a request.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from projectmind import __version__
from projectmind.config import Settings
from projectmind.hook_entry import BUNDLE_HEADER, DAEMON_FILE, prepare_reply, read_event
from projectmind.logging import get_logger
from projectmind.service import MemoryService
from projectmind.storage import open_store

log = get_logger(__name__)

TOKEN_FILE = "daemon.token"
MAX_BODY_BYTES = 4_000_000

#: First background sync waits this long after start-up, so logging in is not slowed.
FIRST_SYNC_DELAY_SECONDS = 120.0

CaptureHandler = Callable[[MemoryService, dict[str, Any]], dict[str, Any]]


def load_token(home: Path) -> str:
    """The shared secret, created on first use and kept stable across restarts."""
    path = home / TOKEN_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    home.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return token


class Daemon:
    def __init__(
        self,
        settings: Settings,
        *,
        port: int | None = None,
        sync_minutes: float | None = None,
        first_sync_delay: float = FIRST_SYNC_DELAY_SECONDS,
    ) -> None:
        self.settings = settings
        self.home = settings.home
        self.token = load_token(self.home)
        self.sync_seconds = (
            sync_minutes if sync_minutes is not None else settings.daemon_sync_minutes
        ) * 60.0
        self.first_sync_delay = first_sync_delay
        self.memory = MemoryService.open(settings)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.sync_running = threading.Event()
        self.capture_handlers: dict[str, CaptureHandler] = {}
        self.last_sync: dict[str, Any] = {}
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", port if port is not None else settings.daemon_port),
            self._handler(),
        )
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self._threads: list[threading.Thread] = []

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Announce, then serve and schedule in background threads."""
        self._announce()
        serving = threading.Thread(target=self.server.serve_forever, name="serve", daemon=True)
        serving.start()
        self._threads.append(serving)
        if self.sync_seconds > 0:
            scheduler = threading.Thread(target=self._schedule, name="sync", daemon=True)
            scheduler.start()
            self._threads.append(scheduler)
        log.info("daemon started", extra={"port": self.port, "pid": os.getpid()})

    def wait(self) -> None:
        self.stop_event.wait()

    def stop(self) -> None:
        self.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        with contextlib.suppress(OSError):
            (self.home / DAEMON_FILE).unlink()
        with self.lock:
            self.memory.close()
        log.info("daemon stopped")

    def _announce(self) -> None:
        info = {
            "pid": os.getpid(),
            "port": self.port,
            "token": self.token,
            "version": __version__,
            "started_at": datetime.now(UTC).isoformat(),
        }
        (self.home / DAEMON_FILE).write_text(json.dumps(info), encoding="utf-8")

    # --- background sync ---------------------------------------------------
    def _schedule(self) -> None:
        if self.stop_event.wait(self.first_sync_delay):
            return
        while True:
            self.run_sync()
            if self.stop_event.wait(self.sync_seconds):
                return

    def run_sync(self) -> bool:
        """One pass over every source. False if one is already running."""
        if self.sync_running.is_set():
            return False
        self.sync_running.set()
        try:
            from projectmind.sources.priority import lower_priority
            from projectmind.sync import sync

            lower_priority()
            store = open_store(self.settings)
            try:
                report = sync(store, self.settings)
            finally:
                store.close()
            self.last_sync = {
                "at": datetime.now(UTC).isoformat(),
                "summary": report.summary(),
            }
        except Exception as exc:
            log.warning("scheduled sync failed", extra={"error": str(exc)})
            self.last_sync = {"at": datetime.now(UTC).isoformat(), "error": str(exc)}
        finally:
            self.sync_running.clear()
        return True

    # --- http --------------------------------------------------------------
    def _handler(self) -> type[BaseHTTPRequestHandler]:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ProjectMind"
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return  # request lines go nowhere; the log file records what matters

            # -- plumbing --------------------------------------------------
            def _reply(
                self,
                status: int,
                body: bytes = b"",
                headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                self._reply(status, json.dumps(payload).encode("utf-8"))

            def _allowed(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                if host not in {f"127.0.0.1:{daemon.port}", f"localhost:{daemon.port}"}:
                    self._json(403, {"error": "bad host"})
                    return False
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {daemon.token}"
                if not hmac.compare_digest(supplied.encode(), expected.encode()):
                    self._json(401, {"error": "unauthorized"})
                    return False
                return True

            def _body(self) -> bytes | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY_BYTES:
                    self._json(413, {"error": "body too large"})
                    return None
                return self.rfile.read(length) if length else b""

            # -- routes ----------------------------------------------------
            def do_GET(self) -> None:
                if self.path == "/health":
                    host = (self.headers.get("Host") or "").lower()
                    if host not in {f"127.0.0.1:{daemon.port}", f"localhost:{daemon.port}"}:
                        self._json(403, {"error": "bad host"})
                        return
                    self._json(200, {"ok": True, "version": __version__})
                    return
                if not self._allowed():
                    return
                if self.path == "/status":
                    with daemon.lock:
                        chunks = daemon.memory.store.count_chunks()
                        records = daemon.memory.store.count_records()
                    self._json(
                        200,
                        {
                            "ok": True,
                            "version": __version__,
                            "pid": os.getpid(),
                            "chunks": chunks,
                            "records": records,
                            "syncing": daemon.sync_running.is_set(),
                            "last_sync": daemon.last_sync,
                        },
                    )
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._allowed():
                    return
                body = self._body()
                if body is None:
                    return
                route = self.path
                try:
                    if route == "/context":
                        self._context(body)
                    elif route == "/delivered":
                        self._delivered(body)
                    elif route == "/sync":
                        started = threading.Thread(target=daemon.run_sync, daemon=True)
                        started.start()
                        self._json(202, {"started": True})
                    elif route == "/shutdown":
                        self._json(200, {"stopping": True})
                        threading.Thread(target=daemon.stop, daemon=True).start()
                    elif route.startswith("/capture/"):
                        self._capture(route.removeprefix("/capture/"), body)
                    else:
                        self._json(404, {"error": "not found"})
                except Exception as exc:  # a bad request must not take the daemon down
                    log.warning("request failed", extra={"route": route, "error": str(exc)})
                    self._json(500, {"error": "internal error"})

            def _context(self, body: bytes) -> None:
                event = read_event(body)
                with daemon.lock:
                    reply = prepare_reply(event, memory=daemon.memory)
                if not reply.payload:
                    self._reply(204)
                    return
                headers = {BUNDLE_HEADER: str(reply.bundle_id)} if reply.bundle_id else {}
                self._reply(200, reply.payload, headers)

            def _delivered(self, body: bytes) -> None:
                data = read_event(body)
                try:
                    bundle_id = UUID(str(data.get("bundle_id")))
                except ValueError:
                    self._json(400, {"error": "bad bundle id"})
                    return
                with daemon.lock:
                    daemon.memory.mark_delivered(bundle_id, bool(data.get("delivered")))
                self._json(200, {"ok": True})

            def _capture(self, kind: str, body: bytes) -> None:
                handler = daemon.capture_handlers.get(kind)
                if handler is None:
                    self._json(404, {"error": f"no capture handler for {kind}"})
                    return
                with daemon.lock:
                    result = handler(daemon.memory, read_event(body))
                self._json(200, result)

        return Handler
