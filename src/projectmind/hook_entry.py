"""Slim entry point for coding-agent hooks.

A hook runs before every prompt, so its cost is paid hundreds of times a day.
This module imports only the standard library at the top level. The installed
hook command is:

    python -I -m projectmind.hook_entry user-prompt-submit

and it works in two tiers:

1. **Fast path.** If the ProjectMind daemon is running it already has the
   database open and the retrieval index warm, so the hook sends it the prompt
   over localhost and prints the reply. A few milliseconds beyond interpreter
   start-up.
2. **In-process fallback.** If there is no daemon (or it does not answer in
   time) the hook does the work itself: imports the service, opens the store,
   retrieves. Slower, never wrong.

Rules, because a misbehaving hook breaks the user's editor rather than this tool:

1. **Never raise.** Every failure path exits 0 with empty output.
2. **Bytes in, bytes out.** On Windows a piped stdout is the ANSI code page, so
   text written through it reaches the model as mojibake or raises. Stdin is
   decoded and stdout encoded as UTF-8 explicitly, and the reply is ASCII-only
   JSON so it survives any transport.
3. **Silent stderr.** Diagnostics go to the log file only.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

#: How long the hook waits for the daemon before doing the work itself.
DAEMON_TIMEOUT_SECONDS = 2.5
DELIVERY_TIMEOUT_SECONDS = 0.8
DAEMON_FILE = "daemon.json"
BUNDLE_HEADER = "X-Projectmind-Bundle"
NO_DAEMON_ENV = "PROJECTMIND_NO_DAEMON"
#: Written by `projectmind schedule install`: the user wants the daemon always on.
AUTOSTART_FILE = "autostart"
REVIVE_STAMP = "daemon.revive"
#: At most one restart attempt in this many seconds, however many prompts arrive.
REVIVE_EVERY_SECONDS = 300.0


def read_event(raw: bytes) -> dict[str, Any]:
    """Parse a hook payload. Never raises; an unreadable payload is empty."""
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
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


@dataclass(frozen=True, slots=True)
class Reply:
    """What the hook will write, and which logged bundle it belongs to."""

    payload: bytes = b""
    bundle_id: UUID | None = None


# --------------------------------------------------------------------------- #
# Tier 1: the daemon
# --------------------------------------------------------------------------- #


def _home() -> Path:
    configured = os.environ.get("PROJECTMIND_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".projectmind"


def daemon_info(home: Path | None = None) -> dict[str, Any] | None:
    """Where the daemon is listening, or None if it has not announced itself."""
    try:
        info = json.loads(((home or _home()) / DAEMON_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(info, dict) and info.get("port") and info.get("token"):
        return info
    return None


def _post(
    info: dict[str, Any], path: str, body: bytes, timeout: float
) -> tuple[int, bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=timeout)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {info['token']}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        data = response.read()
        return response.status, data, {key: value for key, value in response.getheaders()}
    finally:
        connection.close()


def daemon_alive(timeout: float = 0.5) -> bool:
    """Whether a daemon answers, not merely whether one announced itself once."""
    info = daemon_info()
    if info is None:
        return False
    connection = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=timeout)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def revive_daemon(home: Path | None = None) -> bool:
    """Start the daemon again if the user wants it always on and it is not running.

    The logon launcher starts it once per login. If it stops in between (a crash, an
    update, `daemon stop`), the next prompt brings it back; this prompt is answered
    in process meanwhile. Detached and windowless, at most once every few minutes.
    """
    home = home or _home()
    if not (home / AUTOSTART_FILE).is_file():
        return False
    stamp = home / REVIVE_STAMP
    try:
        import time

        if time.time() - stamp.stat().st_mtime < REVIVE_EVERY_SECONDS:
            return False
    except OSError:
        pass
    try:
        import subprocess

        stamp.write_text("", encoding="utf-8")
        interpreter = Path(sys.executable)
        windowless = interpreter.with_name("pythonw.exe")
        command = [str(windowless if windowless.exists() else interpreter), "-I", "-m",
                   "projectmind.cli", "serve"]  # fmt: skip
        flags = 0
        if sys.platform == "win32":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        subprocess.Popen(
            command,
            cwd=str(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
    except Exception:
        return False
    return True


def ask_daemon(raw: bytes) -> Reply | None:
    """The reply from a running daemon, or None to fall back to doing it ourselves."""
    if os.environ.get(NO_DAEMON_ENV):
        return None
    info = daemon_info()
    if info is None:
        revive_daemon()
        return None
    try:
        status, body, headers = _post(info, "/context", raw, DAEMON_TIMEOUT_SECONDS)
    except ConnectionRefusedError:
        revive_daemon()  # announced but gone: it crashed or the machine slept and woke
        return None
    except (OSError, http.client.HTTPException):
        return None
    if status == 204:
        return Reply()
    if status != 200:
        return None
    identifier = headers.get(BUNDLE_HEADER) or headers.get(BUNDLE_HEADER.lower())
    try:
        bundle_id = UUID(identifier) if identifier else None
    except ValueError:
        bundle_id = None
    return Reply(payload=body, bundle_id=bundle_id)


def confirm_with_daemon(bundle_id: UUID, delivered: bool) -> bool:
    info = daemon_info()
    if info is None:
        return False
    body = json.dumps({"bundle_id": str(bundle_id), "delivered": delivered}).encode("utf-8")
    try:
        status, _, _ = _post(info, "/delivered", body, DELIVERY_TIMEOUT_SECONDS)
    except (OSError, http.client.HTTPException):
        return False
    return status == 200


# --------------------------------------------------------------------------- #
# Tier 2: in-process
# --------------------------------------------------------------------------- #


def _quiet_logging() -> Any:
    from projectmind.logging import configure, get_logger

    configure(console=False)
    return get_logger("projectmind.hook")


def prepare_reply(
    event: dict[str, Any],
    *,
    prompt: str | None = None,
    project: Path | None = None,
    memory: Any = None,
) -> Reply:
    """Build the reply for one prompt, using `memory` if given or opening our own."""
    log = _quiet_logging()
    text = prompt or str(event.get("prompt") or "")
    if not text.strip():
        return Reply()
    cwd = project or Path(str(event.get("cwd") or Path.cwd()))
    session = event.get("session_id")

    try:
        if memory is None:
            from projectmind.config import get_settings
            from projectmind.service import MemoryService

            with MemoryService.open(get_settings()) as owned:
                bundle = owned.get_context(
                    text, cwd, source="hook", session_id=str(session) if session else None
                )
        else:
            bundle = memory.get_context(
                text, cwd, source="hook", session_id=str(session) if session else None
            )
    except Exception as exc:  # a broken memory layer must not break the session
        log.warning("hook failed, serving nothing", extra={"error": str(exc)})
        return Reply()

    if bundle.is_empty:
        log.info(
            "hook served nothing",
            extra={"project": bundle.project_key, "reason": bundle.gate_reason},
        )
        return Reply()

    log.info(
        "hook served context",
        extra={
            "bundle": str(bundle.bundle_id),
            "project": bundle.project_key,
            "tokens": bundle.total_tokens,
            "profile": len(bundle.profile),
            "episodic": len(bundle.episodic),
            "evidence": len(bundle.evidence),
        },
    )
    payload = render_reply(f"{bundle.render()}\n\n{bundle_marker(bundle.bundle_id)}")
    return Reply(payload=payload, bundle_id=bundle.bundle_id)


def user_prompt_submit(
    event: dict[str, Any], *, prompt: str | None = None, project: Path | None = None
) -> bytes:
    """The reply bytes for one prompt. Empty when there is nothing to inject."""
    return prepare_reply(event, prompt=prompt, project=project).payload


def confirm_delivery(bundle_id: UUID, delivered: bool) -> None:
    """Record that the written reply did (or did not) reach the agent."""
    if confirm_with_daemon(bundle_id, delivered):
        return
    log = _quiet_logging()
    try:
        from projectmind.config import get_settings
        from projectmind.service import MemoryService

        with MemoryService.open(get_settings()) as memory:
            memory.mark_delivered(bundle_id, delivered)
    except Exception as exc:
        log.warning("could not confirm delivery", extra={"error": str(exc)})


def session_start(event: dict[str, Any], *, project: Path | None = None) -> None:
    """Warm the fingerprint cache for a project. Prints nothing."""
    log = _quiet_logging()
    cwd = project or Path(str(event.get("cwd") or Path.cwd()))
    try:
        from projectmind.config import get_settings
        from projectmind.service import MemoryService

        with MemoryService.open(get_settings()) as memory:
            resolved = memory.resolve_project(cwd)
        log.info("warmed project", extra={"project": resolved.key})
    except Exception as exc:
        log.warning("session-start warm-up failed", extra={"error": str(exc)})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    try:
        raw = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    except (OSError, ValueError):
        raw = b""

    try:
        if command == "user-prompt-submit":
            reply = ask_daemon(raw)
            if reply is None:
                reply = prepare_reply(read_event(raw))
            if reply.payload:
                delivered = False
                try:
                    sys.stdout.buffer.write(reply.payload)
                    sys.stdout.buffer.flush()
                    delivered = True
                finally:
                    if reply.bundle_id is not None:
                        confirm_delivery(reply.bundle_id, delivered)
        elif command == "session-start":
            if not os.environ.get(NO_DAEMON_ENV) and not daemon_alive():
                revive_daemon()
            session_start(read_event(raw))
    except Exception as exc:  # last line of defence: the editor must never see this
        with contextlib.suppress(Exception):
            _quiet_logging().warning("hook crashed, serving nothing", extra={"error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
