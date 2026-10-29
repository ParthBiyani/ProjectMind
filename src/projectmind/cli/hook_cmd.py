"""`projectmind hook ...`: the same hook entry points, reachable from the CLI.

The installed hooks call `python -I -m projectmind.hook_entry` directly, which
skips importing this whole CLI. These commands exist for trying a hook by hand
(`--prompt`, `--project`) and share every line of behaviour with the real thing,
so what you see here is exactly what the agent receives.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from projectmind import hook_entry

app = typer.Typer(help="Entry points for coding-agent hooks.", no_args_is_help=True)


def _event() -> dict[str, object]:
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""
    return hook_entry.read_event(raw)


@app.command("user-prompt-submit")
def user_prompt_submit(
    prompt: str = typer.Option("", help="Override the prompt instead of reading stdin."),
    project: Path | None = typer.Option(None, help="Override the project directory."),
) -> None:
    """Print the hook reply for a prompt, or nothing.

    Silence is the common case and the correct one for trivial prompts.
    """
    reply = hook_entry.user_prompt_submit(_event(), prompt=prompt or None, project=project)
    if reply:
        sys.stdout.buffer.write(reply)
        sys.stdout.buffer.flush()


@app.command("session-start")
def session_start(
    project: Path | None = typer.Option(None, help="Override the project directory."),
) -> None:
    """Warm the cache for a project so the first prompt of a session is not slow.

    Prints nothing. A session-start hook that writes into the context window is
    a session-start hook that gets uninstalled.
    """
    hook_entry.session_start(_event(), project=project)
