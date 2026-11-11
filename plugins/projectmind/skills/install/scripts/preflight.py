"""What the machine already has, before ProjectMind is installed. Standard library only.

Prints one JSON object. It reads nothing from the user's files: it looks for programs,
versions and the two folders ProjectMind uses, and reports what it found.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 11)


def run(*command: str) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or done.stderr).strip().splitlines()[0] if done.returncode == 0 else None


def find_python() -> dict[str, object]:
    """The interpreter running this script, and whether it is new enough."""
    version = sys.version_info[:3]
    return {
        "path": sys.executable,
        "version": ".".join(map(str, version)),
        "ok": version >= MINIMUM_PYTHON,
    }


def main() -> None:
    home = Path(os.environ.get("PROJECTMIND_HOME") or Path.home() / ".projectmind")
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    installed = run(sys.executable, "-m", "pip", "show", "projectmind")
    report = {
        "os": platform.system(),
        "windows": sys.platform == "win32",
        "python": find_python(),
        "python_required": ".".join(map(str, MINIMUM_PYTHON)) + "+",
        "git": shutil.which("git"),
        "pipx": shutil.which("pipx"),
        "projectmind_installed": installed is not None,
        "projectmind_command": shutil.which("projectmind"),
        "projectmind_home": {"path": str(home), "exists": home.is_dir()},
        "claude_config": {
            "path": str(claude),
            "exists": claude.is_dir(),
            "settings_json": (claude / "settings.json").is_file(),
        },
        "capabilities": {
            "core (index, hook, MCP, daemon)": True,
            "start at logon": sys.platform == "win32",
            "browser history and page capture": sys.platform == "win32",
        },
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
