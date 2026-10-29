"""What `projectmind doctor` checks, as plain functions that return findings.

The old doctor reported "claude code: ok" whenever a settings file existed, which
says nothing about whether ProjectMind is wired into it. Every check here looks
at the thing it names, and the hook check actually runs the hook.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from projectmind.cli.install_cmd import (
    MARKER,
    claude_mcp_path,
    claude_settings_path,
    is_ours,
)
from projectmind.config import Settings
from projectmind.storage import open_store

#: A hook that costs more than this is felt on every prompt.
HOOK_BUDGET_SECONDS = 1.0
HOOK_TIMEOUT_SECONDS = 30.0

SELF_TEST_PROMPT = "Add Supabase auth to a Flutter screen and wire the session provider"


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Finding:
    name: str
    status: Status
    detail: str


def check_home(settings: Settings) -> Finding:
    if settings.home.exists():
        return Finding("home directory", Status.OK, str(settings.home))
    return Finding("home directory", Status.WARN, f"{settings.home} does not exist; run init")


def check_storage(settings: Settings) -> list[Finding]:
    try:
        store = open_store(settings)
        try:
            version = store.migrate()
            records = store.count_records()
            statements = len(store.list_statements())
        finally:
            store.close()
    except Exception as exc:  # doctor reports failures instead of raising them
        return [Finding("storage", Status.FAIL, str(exc))]
    return [
        Finding("storage", Status.OK, f"{settings.describe_backend()} at schema v{version}"),
        Finding(
            "memory",
            Status.OK if statements else Status.WARN,
            f"{statements} profile statements, {records} episodic records"
            + ("" if statements else "; run: projectmind profile seed"),
        ),
    ]


def check_mcp_sdk() -> Finding:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        return Finding("mcp sdk", Status.WARN, "pip install 'projectmind[mcp]'")
    return Finding("mcp sdk", Status.OK, "installed")


def check_embeddings(settings: Settings) -> Finding:
    from projectmind.storage.embeddings import build_embedder

    embedder = build_embedder(settings)
    detail = f"{embedder.name}, {embedder.dimensions} dimensions"
    if embedder.name == "fastembed":
        return Finding(
            "embeddings",
            Status.WARN,
            f"{detail}; loads a model on every hook call and the retrieval thresholds "
            "are calibrated for the hashing embedder. Set PROJECTMIND_EMBEDDING_PROVIDER=hashing",
        )
    return Finding("embeddings", Status.OK, detail)


def check_llm(settings: Settings) -> Finding:
    if settings.has_llm:
        return Finding("llm notes", Status.OK, "ANTHROPIC_API_KEY is set")
    return Finding("llm notes", Status.WARN, "off; heuristics only (set ANTHROPIC_API_KEY)")


def check_git() -> Finding:
    path = shutil.which("git")
    if path:
        return Finding("git", Status.OK, path)
    return Finding("git", Status.FAIL, "not found on PATH; ingestion needs it")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def check_hooks() -> list[Finding]:
    path = claude_settings_path()
    config = _read_json(path)
    if config is None:
        return [
            Finding(
                "claude hooks",
                Status.WARN,
                f"no readable {path}; run: projectmind install claude-code",
            )
        ]

    hooks = config.get("hooks") or {}
    findings: list[Finding] = []
    for event in ("UserPromptSubmit", "SessionStart"):
        entries = [entry for entry in hooks.get(event) or [] if is_ours(entry)]
        if not entries:
            findings.append(
                Finding(
                    f"hook {event}",
                    Status.WARN,
                    "not installed; run: projectmind install claude-code",
                )
            )
            continue
        interpreter = _interpreter_in(entries[0])
        if interpreter is not None and not Path(interpreter).exists():
            findings.append(
                Finding(
                    f"hook {event}",
                    Status.FAIL,
                    f"points at {interpreter}, which no longer exists; reinstall",
                )
            )
        else:
            findings.append(Finding(f"hook {event}", Status.OK, f"installed in {path}"))
    return findings


def _commands(entry: Any) -> list[str]:
    """Every `command` string inside a hook entry."""
    found: list[str] = []
    if isinstance(entry, dict):
        for key, value in entry.items():
            if key == "command" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_commands(value))
    elif isinstance(entry, list):
        for item in entry:
            found.extend(_commands(item))
    return found


def _interpreter_in(entry: Any) -> str | None:
    """The quoted interpreter path that starts a hook command, if there is one."""
    for command in _commands(entry):
        stripped = command.strip()
        if stripped.startswith('"') and stripped.count('"') >= 2:
            return stripped[1 : stripped.index('"', 1)]
    return None


def check_mcp_registration() -> Finding:
    path = claude_mcp_path()
    config = _read_json(path)
    if config is None:
        return Finding(
            "mcp server", Status.WARN, f"no readable {path}; run: projectmind install claude-code"
        )
    if MARKER in (config.get("mcpServers") or {}):
        return Finding("mcp server", Status.OK, f"registered in {path}")
    return Finding(
        "mcp server", Status.WARN, "not registered; run: projectmind install claude-code"
    )


def check_hook_runs(settings: Settings) -> Finding:
    """Run the hook exactly as the installer wires it, and judge what it prints.

    A canned payload, an isolated interpreter and a scratch working directory:
    the same conditions a hook sees inside someone else's project. History is
    switched off for the run so the self-test does not appear as a real prompt.
    """
    payload = json.dumps(
        {"prompt": SELF_TEST_PROMPT, "cwd": tempfile.gettempdir(), "session_id": "doctor"}
    ).encode("utf-8")
    env = {
        **os.environ,
        "PROJECTMIND_HOME": str(settings.home),
        "PROJECTMIND_HISTORY": "off",
        # Test the hook's own path; the daemon is checked separately.
        "PROJECTMIND_NO_DAEMON": "1",
    }
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "projectmind.hook_entry", "user-prompt-submit"],
            input=payload,
            capture_output=True,
            env=env,
            cwd=tempfile.gettempdir(),
            timeout=HOOK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Finding(
            "hook self-test", Status.FAIL, f"no answer within {HOOK_TIMEOUT_SECONDS:.0f}s"
        )
    elapsed = time.perf_counter() - started

    if result.returncode != 0:
        return Finding("hook self-test", Status.FAIL, f"exited {result.returncode}")
    if result.stderr.strip():
        return Finding(
            "hook self-test",
            Status.FAIL,
            f"wrote to stderr, which a client may show: {result.stderr[:120]!r}",
        )
    try:
        result.stdout.decode("utf-8")
        reply = json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        return Finding("hook self-test", Status.FAIL, "output was not valid UTF-8 JSON")

    served = "context injected" if reply else "nothing injected (empty memory is a valid answer)"
    speed = f"{elapsed * 1000:.0f} ms"
    if elapsed > HOOK_BUDGET_SECONDS:
        return Finding(
            "hook self-test", Status.WARN, f"{served}, but {speed} is slow for every prompt"
        )
    return Finding("hook self-test", Status.OK, f"{served}, {speed}")


def check_daemon(settings: Settings) -> Finding:
    """Is the daemon up, and does a hook get answered quickly through it?"""
    from projectmind.hook_entry import daemon_info

    info = daemon_info(settings.home)
    if info is None:
        return Finding(
            "daemon",
            Status.WARN,
            "not running; hooks work but are slower. Start it with: projectmind serve "
            "(or at logon: projectmind schedule install)",
        )
    import http.client

    started = time.perf_counter()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=3)
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        connection.close()
    except (OSError, http.client.HTTPException) as exc:
        return Finding("daemon", Status.WARN, f"announced on port {info['port']} but silent: {exc}")
    if response.status != 200:
        return Finding("daemon", Status.WARN, f"answered {response.status}")
    return Finding(
        "daemon", Status.OK, f"port {info['port']}, {(time.perf_counter() - started) * 1000:.0f} ms"
    )


def run_all(settings: Settings) -> list[Finding]:
    findings = [check_home(settings), *check_storage(settings)]
    findings += [
        check_mcp_sdk(),
        check_embeddings(settings),
        check_llm(settings),
        check_git(),
        *check_hooks(),
        check_mcp_registration(),
        check_hook_runs(settings),
        check_daemon(settings),
    ]
    return findings
