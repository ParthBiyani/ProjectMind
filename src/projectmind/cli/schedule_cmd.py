"""`projectmind schedule ...`: start the daemon at logon (Windows Task Scheduler).

One task, run as you, with limited rights, started when you log in. It launches
the daemon without a console window. Nothing here needs administrator rights,
and `schedule remove` takes it away again.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from projectmind.cli._common import fail, note, ok

app = typer.Typer(help="Start the daemon automatically at logon.", no_args_is_help=True)

TASK_NAME = "ProjectMind Daemon"


def _windows_only() -> None:
    if sys.platform != "win32":
        fail("scheduling is implemented for Windows (Task Scheduler) only")
        note("elsewhere, start it from a systemd user unit or launchd: projectmind serve")
        raise typer.Exit(code=1)


def daemon_command() -> str:
    """The command the task runs: the windowless interpreter if there is one."""
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    chosen = windowless if windowless.exists() else interpreter
    return f'"{chosen}" -I -m projectmind.cli serve'


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, check=False, timeout=30
    )


@app.command("install")
def install() -> None:
    """Register the daemon to start whenever you log in."""
    _windows_only()
    result = _run(
        "/Create", "/TN", TASK_NAME, "/TR", daemon_command(),
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    )  # fmt: skip
    if result.returncode != 0:
        fail(result.stderr.strip() or result.stdout.strip() or "schtasks failed")
        raise typer.Exit(code=1)
    ok(f"registered '{TASK_NAME}' to start at logon")
    note("it starts the next time you log in; to start it now: projectmind serve")


@app.command("remove")
def remove() -> None:
    """Take the logon task away."""
    _windows_only()
    result = _run("/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode != 0:
        fail(result.stderr.strip() or "no such task")
        raise typer.Exit(code=1)
    ok(f"removed '{TASK_NAME}'")


@app.command("status")
def status() -> None:
    """Is the logon task registered?"""
    _windows_only()
    result = _run("/Query", "/TN", TASK_NAME)
    if result.returncode != 0:
        note("not registered; run: projectmind schedule install")
        raise typer.Exit(code=1)
    ok(f"'{TASK_NAME}' is registered")
