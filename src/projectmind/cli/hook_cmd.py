"""`projectmind hook ...` — the entry points a coding agent's hooks call.

This is what turns a pull-based MCP tool into something that behaves like
default injection. Claude Code runs a `UserPromptSubmit` hook before the model
sees the prompt and appends whatever the hook prints to the context, so the
agent receives memory without having to know memory exists.

Three rules govern everything in this module, because a hook that misbehaves
breaks the user's editor rather than just this tool:

1. **Never raise.** Every failure path exits 0 with empty output.
2. **Never print anything but context.** Diagnostics go to the log file, never
   to stdout, and never to stderr where the client might surface them.
3. **Never block.** The work is bounded and the client's own timeout is the
   backstop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from projectmind.config import get_settings
from projectmind.logging import get_logger
from projectmind.service import MemoryService

log = get_logger(__name__)

app = typer.Typer(help="Entry points for coding-agent hooks.", no_args_is_help=True)


def _read_event() -> dict[str, Any]:
    """Parse the hook payload from stdin. Never raises."""
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("hook payload was not json")
        return {}
    return parsed if isinstance(parsed, dict) else {}


@app.command("user-prompt-submit")
def user_prompt_submit(
    prompt: str = typer.Option("", help="Override the prompt instead of reading stdin."),
    project: Path | None = typer.Option(None, help="Override the project directory."),
) -> None:
    """Print the context block for a prompt, or nothing.

    Wired to the client's pre-prompt hook. Silence is the common case and the
    correct one for trivial prompts.
    """
    event = _read_event()
    text = prompt or str(event.get("prompt") or "")
    cwd = project or Path(str(event.get("cwd") or Path.cwd()))

    if not text.strip():
        return

    try:
        settings = get_settings()
        with MemoryService.open(settings) as memory:
            bundle = memory.get_context(text, cwd)
    except Exception as exc:  # a broken memory layer must not break the session
        log.warning("hook failed, serving nothing", extra={"error": str(exc)})
        return

    if bundle.is_empty:
        log.info(
            "hook served nothing",
            extra={"project": bundle.project_key, "reason": bundle.gate_reason},
        )
        return

    log.info(
        "hook served context",
        extra={
            "bundle": str(bundle.bundle_id),
            "project": bundle.project_key,
            "tokens": bundle.total_tokens,
            "profile": len(bundle.profile),
            "episodic": len(bundle.episodic),
        },
    )
    sys.stdout.write(bundle.render())


@app.command("session-start")
def session_start(
    project: Path | None = typer.Option(None, help="Override the project directory."),
) -> None:
    """Warm the cache for a project so the first prompt of a session is not slow.

    Prints nothing. A session-start hook that writes into the context window is
    a session-start hook that gets uninstalled.
    """
    event = _read_event()
    cwd = project or Path(str(event.get("cwd") or Path.cwd()))
    try:
        with MemoryService.open(get_settings()) as memory:
            resolved = memory.resolve_project(cwd)
        log.info("warmed project", extra={"project": resolved.key})
    except Exception as exc:
        log.warning("session-start warm-up failed", extra={"error": str(exc)})
