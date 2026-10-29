"""`projectmind serve` and `projectmind daemon ...`: the always-on part.

Run once at logon (see `projectmind schedule install`), the daemon keeps the
database warm so hooks answer in a few milliseconds, re-reads your sources on a
timer so memory keeps growing, and accepts captures from the browser extension.
"""

from __future__ import annotations

import http.client
import json
import signal
import sys
from types import FrameType

import typer

from projectmind.cli._common import console, fail, note, ok, table
from projectmind.config import get_settings
from projectmind.hook_entry import daemon_info

app = typer.Typer(help="Inspect and control the local daemon.", no_args_is_help=True)


def _request(method: str, path: str, *, timeout: float = 3.0) -> tuple[int, dict[str, object]]:
    info = daemon_info(get_settings().home)
    if info is None:
        raise ConnectionError("the daemon has not announced itself (it is not running)")
    connection = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=timeout)
    try:
        connection.request(
            method,
            path,
            body=b"{}" if method == "POST" else None,
            headers={"Authorization": f"Bearer {info['token']}"},
        )
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}
    finally:
        connection.close()


def is_running() -> bool:
    try:
        status, _ = _request("GET", "/status")
    except (OSError, ConnectionError, http.client.HTTPException, ValueError):
        return False
    return status == 200


def serve(
    port: int | None = typer.Option(None, help="Port to listen on. Default: a free one."),
    sync_minutes: float | None = typer.Option(
        None, help="Minutes between background syncs. 0 turns the scheduler off."
    ),
    first_sync_delay: float = typer.Option(
        120.0, help="Seconds to wait before the first background sync."
    ),
) -> None:
    """Run the daemon in the foreground. Stop it with Ctrl+C or `daemon stop`."""
    from projectmind.daemon import Daemon
    from projectmind.sources.priority import lower_priority

    if is_running():
        fail("the daemon is already running; see: projectmind daemon status")
        raise typer.Exit(code=1)

    lower_priority()
    daemon = Daemon(
        get_settings(),
        port=port,
        sync_minutes=sync_minutes,
        first_sync_delay=first_sync_delay,
    )

    def shutdown(_signal: int, _frame: FrameType | None) -> None:
        daemon.stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    daemon.start()
    ok(f"daemon listening on 127.0.0.1:{daemon.port}")
    note(
        "background sync "
        + (f"every {daemon.sync_seconds / 60:g} min" if daemon.sync_seconds else "off")
    )
    sys.stdout.flush()
    try:
        daemon.wait()
    finally:
        daemon.stop()


@app.command("status")
def status() -> None:
    """Is the daemon up, and what has it been doing?"""
    try:
        code, data = _request("GET", "/status")
    except (OSError, ConnectionError, http.client.HTTPException, ValueError) as exc:
        fail(f"not running ({exc})")
        raise typer.Exit(code=1) from exc
    if code != 200:
        fail(f"unexpected answer: {code}")
        raise typer.Exit(code=1)
    rendered = table("", "")
    rendered.add_row("pid", str(data.get("pid")))
    rendered.add_row("version", str(data.get("version")))
    rendered.add_row("history records", str(data.get("records")))
    rendered.add_row("source passages", str(data.get("chunks")))
    rendered.add_row("syncing now", "yes" if data.get("syncing") else "no")
    last = data.get("last_sync") or {}
    if isinstance(last, dict) and last:
        rendered.add_row(
            "last sync", f"{last.get('at')} {last.get('summary') or last.get('error')}"
        )
    console.print(rendered)


@app.command("stop")
def stop() -> None:
    """Ask the daemon to shut down."""
    try:
        _request("POST", "/shutdown")
    except (OSError, ConnectionError, http.client.HTTPException, ValueError) as exc:
        fail(f"not running ({exc})")
        raise typer.Exit(code=1) from exc
    ok("daemon is stopping")


@app.command("sync")
def sync_now() -> None:
    """Ask the daemon to run a sync pass now, in the background."""
    try:
        code, _ = _request("POST", "/sync")
    except (OSError, ConnectionError, http.client.HTTPException, ValueError) as exc:
        fail(f"not running ({exc})")
        raise typer.Exit(code=1) from exc
    ok("sync started" if code == 202 else f"unexpected answer: {code}")


@app.command("token")
def token() -> None:
    """Print the secret the browser extension needs to talk to the daemon."""
    from projectmind.daemon import load_token

    console.print(load_token(get_settings().ensure_home()), markup=False, soft_wrap=True)
