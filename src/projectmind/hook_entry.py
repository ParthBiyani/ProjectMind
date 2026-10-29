"""Slim entry point for coding-agent hooks.

A hook runs before every prompt, so its cost is paid hundreds of times a day.
Going through the full Typer CLI imports evaluation, ingestion and reflection
code that a hook never touches. This module imports only the service, and is
what the installer points `UserPromptSubmit` and `SessionStart` at:

    python -I -m projectmind.hook_entry user-prompt-submit

Rules, because a misbehaving hook breaks the user's editor rather than this
tool:

1. **Never raise.** Every failure path exits 0 with empty output.
2. **Bytes in, bytes out.** On Windows a piped stdout is the ANSI code page, so
   text written through it reaches the model as mojibake or raises. Stdin is
   decoded and stdout encoded as UTF-8 explicitly, and the reply is ASCII-only
   JSON so it survives any transport.
3. **Silent stderr.** Diagnostics go to the log file only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from projectmind.logging import configure

configure(console=False)

from projectmind.config import get_settings  # noqa: E402
from projectmind.logging import get_logger  # noqa: E402

log = get_logger(__name__)


def read_event(raw: bytes) -> dict[str, Any]:
    """Parse a hook payload. Never raises; an unreadable payload is empty."""
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.debug("hook payload was not json")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def bundle_marker(bundle_id: object) -> str:
    """The id an agent passes back to `memory_feedback`."""
    return f"<!-- projectmind bundle {bundle_id} -->"


def render_reply(context: str) -> bytes:
    """The hook reply: ASCII-only JSON carrying the context, or nothing."""
    if not context.strip():
        return b""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    return json.dumps(payload, ensure_ascii=True).encode("ascii")


def user_prompt_submit(
    event: dict[str, Any], *, prompt: str | None = None, project: Path | None = None
) -> bytes:
    """The reply bytes for one prompt. Empty when there is nothing to inject."""
    text = prompt or str(event.get("prompt") or "")
    if not text.strip():
        return b""
    cwd = project or Path(str(event.get("cwd") or Path.cwd()))

    try:
        from projectmind.service import MemoryService

        with MemoryService.open(get_settings()) as memory:
            bundle = memory.get_context(text, cwd)
    except Exception as exc:  # a broken memory layer must not break the session
        log.warning("hook failed, serving nothing", extra={"error": str(exc)})
        return b""

    if bundle.is_empty:
        log.info(
            "hook served nothing",
            extra={"project": bundle.project_key, "reason": bundle.gate_reason},
        )
        return b""

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
    return render_reply(f"{bundle.render()}\n\n{bundle_marker(bundle.bundle_id)}")


def session_start(event: dict[str, Any], *, project: Path | None = None) -> None:
    """Warm the fingerprint cache for a project. Prints nothing."""
    cwd = project or Path(str(event.get("cwd") or Path.cwd()))
    try:
        from projectmind.service import MemoryService

        with MemoryService.open(get_settings()) as memory:
            resolved = memory.resolve_project(cwd)
        log.info("warmed project", extra={"project": resolved.key})
    except Exception as exc:
        log.warning("session-start warm-up failed", extra={"error": str(exc)})


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    try:
        raw = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    except (OSError, ValueError):
        raw = b""
    event = read_event(raw)

    try:
        if command == "user-prompt-submit":
            reply = user_prompt_submit(event)
            if reply:
                sys.stdout.buffer.write(reply)
                sys.stdout.buffer.flush()
        elif command == "session-start":
            session_start(event)
    except Exception as exc:  # last line of defence: the editor must never see this
        log.warning("hook crashed, serving nothing", extra={"error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
