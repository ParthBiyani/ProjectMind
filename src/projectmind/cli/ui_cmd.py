"""`projectmind ui`: the dashboard, in your browser.

The daemon serves it at http://127.0.0.1:<port>/ui/. This command opens it with the
daemon's token in the URL fragment, which the browser keeps to itself (a fragment is
never sent to a server); the page moves it into session storage and clears the
address bar.

`--demo` serves the fictional workspace from `projectmind demo` instead, fully
indexed, on a free port, so a demo never shows anything personal.
"""

from __future__ import annotations

import signal
import sys
import webbrowser
from types import FrameType

import typer

from projectmind.cli._common import fail, note, ok
from projectmind.cli.daemon_cmd import is_running
from projectmind.config import get_settings
from projectmind.hook_entry import daemon_info

#: Proposals for the demo's Profile view. Proposed only: nothing proposed is ever
#: served, so they cannot change what the tour shows.
DEMO_PROPOSALS = (
    ("Backs off with exponential delay and jitter on HTTP 429, capped at 30 seconds",
     "work_style", 0.86, ("http", "retry")),
    ("Validates time series walk-forward, never with shuffled k-fold", "constraint", 0.9,
     ("validation",)),
    ("Prefers SQLite full-text search over a vector database for small note collections",
     "tool_preference", 0.82, ("sqlite", "fts5")),
    ("Tried a fixed retry delay and abandoned it: every worker retried together",
     "abandoned", 0.88, ("retry",)),
)  # fmt: skip


def _seed_demo_profile(memory: object) -> None:
    from projectmind.models import Category, ProfileStatement, StatementStatus
    from projectmind.service import MemoryService

    assert isinstance(memory, MemoryService)
    memory.profile.add_many(
        ProfileStatement(
            statement=text,
            category=Category(category),
            confidence=confidence,
            status=StatementStatus.PROPOSED,
            entities=entities,
        )
        for text, category, confidence, entities in DEMO_PROPOSALS
    )


def _show(url: str, browser: bool) -> None:
    if browser and webbrowser.open(url):
        ok("opened the dashboard in your browser")
    else:
        note("open this address (it carries the access token; do not share it):")
        print(url)
    sys.stdout.flush()


def _serve_until_stopped(daemon: object, url: str, browser: bool) -> None:
    from projectmind.daemon import Daemon

    assert isinstance(daemon, Daemon)

    def shutdown(_signal: int, _frame: FrameType | None) -> None:
        daemon.stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    daemon.start()
    _show(url, browser)
    note("press Ctrl+C to stop")
    try:
        daemon.wait()
    finally:
        daemon.stop()


def ui(
    demo: bool = typer.Option(
        False, "--demo", help="Serve the fictional demo workspace instead of your memory."
    ),
    browser: bool = typer.Option(True, "--browser/--no-browser", help="Open a browser tab."),
    port: int = typer.Option(0, help="Port for --demo, or for a daemon started here. 0: any."),
) -> None:
    """Open the dashboard: ask, search, history, knowledge, profile and evaluation."""
    from projectmind.daemon import Daemon

    if demo:
        from projectmind.demo import demo_memory

        note("building the demo workspace (a few seconds)")
        with demo_memory() as (memory, paths):
            _seed_demo_profile(memory)
            daemon = Daemon(
                memory.settings,
                port=port,
                sync_minutes=0,
                memory=memory,
                demo=paths,
                announce=False,
            )
            _serve_until_stopped(daemon, daemon.ui_url, browser)
        return

    settings = get_settings()
    if is_running():
        info = daemon_info(settings.home)
        assert info is not None
        _show(f"http://127.0.0.1:{info['port']}/ui/#token={info['token']}", browser)
        return

    note("the daemon is not running; starting it here (it stops when you press Ctrl+C)")
    from projectmind.sources.priority import lower_priority

    lower_priority()
    try:
        daemon = Daemon(settings, port=port or None)
    except OSError as exc:
        fail(f"could not listen ({exc}); try: --port 0")
        raise typer.Exit(code=1) from exc
    _serve_until_stopped(daemon, daemon.ui_url, browser)
