"""Child processes must not open console windows.

The daemon runs under pythonw, which has no console. A console program it starts
(git, schtasks) then gets a window of its own that flashes on screen and steals focus.
A scan runs hundreds of git calls, so one forgotten flag is felt immediately.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from projectmind.process import NO_WINDOW

SRC = Path(__file__).resolve().parents[1] / "src" / "projectmind"
SPAWNERS = {"run", "Popen", "check_output", "check_call", "call"}


def test_the_flag_is_the_windows_one_and_harmless_elsewhere() -> None:
    expected = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined,unused-ignore]
    assert expected == NO_WINDOW


def test_every_child_process_in_the_package_is_started_without_a_window() -> None:
    missing: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if (
                node.func.attr in SPAWNERS
                and isinstance(owner, ast.Name)
                and owner.id == "subprocess"
                and not any(keyword.arg == "creationflags" for keyword in node.keywords)
            ):
                missing.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}")
    assert not missing, f"subprocess calls without creationflags: {missing}"
