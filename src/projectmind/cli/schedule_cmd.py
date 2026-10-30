"""`projectmind schedule ...`: start the daemon at logon (Windows).

Two ways, tried in order, neither needing administrator rights unless Windows
insists:

1. a Task Scheduler entry that runs at your logon;
2. if Task Scheduler refuses (creating a logon trigger needs elevation on many
   machines), a small hidden launcher in your per-user Startup folder, which
   Windows runs at logon with no elevation.

Either way it starts the daemon without a console window, and `schedule remove`
takes away whichever exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

from projectmind.cli._common import fail, note, ok

app = typer.Typer(help="Start the daemon automatically at logon.", no_args_is_help=True)

TASK_NAME = "ProjectMind Daemon"
LAUNCHER_NAME = "ProjectMind Daemon.vbs"


def _windows_only() -> None:
    if sys.platform != "win32":
        fail("scheduling is implemented for Windows only")
        note("elsewhere, start it from a systemd user unit or launchd: projectmind serve")
        raise typer.Exit(code=1)


def daemon_executable() -> str:
    """The windowless interpreter if there is one."""
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else interpreter)


def daemon_command() -> str:
    return f'"{daemon_executable()}" -I -m projectmind.cli serve'


def startup_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def launcher_path() -> Path:
    return startup_dir() / LAUNCHER_NAME


def launcher_script() -> str:
    """A VBScript that starts the daemon hidden. Window style 0 means no window."""
    command = daemon_command().replace('"', '""')
    return (
        "' Starts the ProjectMind daemon at logon. Remove with: projectmind schedule remove\n"
        'CreateObject("WScript.Shell").Run "' + command + '", 0, False\n'
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, check=False, timeout=30
    )


def _task_registered() -> bool:
    return _run("/Query", "/TN", TASK_NAME).returncode == 0


@app.command("install")
def install() -> None:
    """Make the daemon start whenever you log in."""
    _windows_only()
    result = _run(
        "/Create", "/TN", TASK_NAME, "/TR", daemon_command(),
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    )  # fmt: skip
    if result.returncode == 0:
        ok(f"registered '{TASK_NAME}' in Task Scheduler to start at logon")
        note("it starts the next time you log in; to start it now: projectmind serve")
        return

    reason = (result.stderr.strip() or result.stdout.strip() or "schtasks failed").splitlines()[0]
    note(f"Task Scheduler refused ({reason}); using your Startup folder instead")
    target = launcher_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(launcher_script(), encoding="utf-8")
    except OSError as exc:
        fail(f"could not write {target}: {exc}")
        raise typer.Exit(code=1) from exc
    ok(f"installed {target}")
    note("it starts the next time you log in; to start it now: projectmind serve")


@app.command("remove")
def remove() -> None:
    """Take away whichever logon entry exists."""
    _windows_only()
    removed = []
    if _task_registered() and _run("/Delete", "/TN", TASK_NAME, "/F").returncode == 0:
        removed.append("Task Scheduler entry")
    target = launcher_path()
    if target.exists():
        target.unlink()
        removed.append("Startup launcher")
    if not removed:
        fail("nothing to remove")
        raise typer.Exit(code=1)
    ok("removed " + " and ".join(removed))


@app.command("status")
def status() -> None:
    """Where is the daemon set to start at logon?"""
    _windows_only()
    found = []
    if _task_registered():
        found.append(f"Task Scheduler '{TASK_NAME}'")
    if launcher_path().exists():
        found.append(f"Startup launcher {launcher_path()}")
    if not found:
        note("not set up; run: projectmind schedule install")
        raise typer.Exit(code=1)
    for item in found:
        ok(item)
