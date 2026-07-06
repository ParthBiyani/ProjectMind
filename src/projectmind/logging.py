"""Logging.

Two destinations with different jobs. The console stays quiet at WARNING and
above, because this process runs behind an interactive agent and must never
print into a working session. The rotating file at `PROJECTMIND_HOME/logs` keeps
everything, because a gate decision nobody can inspect afterwards is a gate
decision nobody can fix.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from typing import Any

from projectmind.config import Settings, get_settings

_CONFIGURED = False
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any extra fields folded in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(settings: Settings | None = None, *, force: bool = False) -> None:
    """Install handlers once per process."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings = settings or get_settings()
    root = logging.getLogger("projectmind")
    root.handlers.clear()
    root.setLevel(settings.log_level.upper())
    root.propagate = False

    # stderr, never stdout: stdout is the MCP transport.
    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("projectmind %(levelname)s %(message)s"))
    root.addHandler(console)

    try:
        settings.ensure_home()
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_dir / "projectmind.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except OSError:
        # An unwritable home is not a reason to fail a retrieval.
        root.warning("could not open the log file; continuing with console logging only")

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Child logger under the `projectmind` root."""
    configure()
    suffix = name.removeprefix("projectmind.").removeprefix("projectmind")
    return logging.getLogger(f"projectmind.{suffix}" if suffix else "projectmind")
