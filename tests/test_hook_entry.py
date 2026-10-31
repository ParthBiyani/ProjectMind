from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind import hook_entry
from projectmind.cli import app
from projectmind.config import reset_settings

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "pmhome"
    monkeypatch.setenv("PROJECTMIND_HOME", str(path))
    monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
    monkeypatch.setenv("PROJECTMIND_EMBEDDING_PROVIDER", "hashing")
    reset_settings()
    runner.invoke(app, ["init", "--no-seed"])
    runner.invoke(app, ["profile", "seed", "--activate-all"])
    yield path
    reset_settings()


class TestReadEvent:
    def test_utf8_bytes_are_decoded_regardless_of_the_console_code_page(self) -> None:
        raw = json.dumps({"prompt": "fix the — em dash 要"}, ensure_ascii=False).encode()
        assert hook_entry.read_event(raw)["prompt"] == "fix the — em dash 要"

    def test_invalid_bytes_do_not_raise(self) -> None:
        assert hook_entry.read_event(b"\xff\xfe not json") == {}

    def test_empty_and_non_object_payloads_are_empty(self) -> None:
        assert hook_entry.read_event(b"") == {}
        assert hook_entry.read_event(b"[1, 2]") == {}


class TestReply:
    def test_nothing_to_say_is_zero_bytes(self) -> None:
        assert hook_entry.render_reply("  \n") == b""

    def test_the_reply_is_ascii_only_json_with_the_context(self) -> None:
        reply = hook_entry.render_reply("café — done")
        reply.decode("ascii")  # would raise if any byte were >= 0x80
        body = json.loads(reply)["hookSpecificOutput"]
        assert body["hookEventName"] == "UserPromptSubmit"
        assert body["additionalContext"] == "café — done"


class TestUserPromptSubmit:
    def test_a_real_prompt_gets_context_and_a_bundle_id(self, home: Path) -> None:
        event = {"prompt": "Add Supabase auth to a Flutter screen", "cwd": str(home)}
        reply = hook_entry.user_prompt_submit(event)
        context = json.loads(reply)["hookSpecificOutput"]["additionalContext"]
        assert "## ProjectMind context" in context
        assert "<!-- projectmind bundle " in context

    def test_a_trivial_prompt_is_silent(self, home: Path) -> None:
        assert hook_entry.user_prompt_submit({"prompt": "rename this variable"}) == b""

    def test_an_empty_prompt_is_silent(self, home: Path) -> None:
        assert hook_entry.user_prompt_submit({"prompt": ""}) == b""

    def test_a_broken_store_serves_nothing_instead_of_raising(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("store is gone")

        monkeypatch.setattr("projectmind.service.MemoryService.open", boom)
        assert hook_entry.user_prompt_submit({"prompt": "Add Supabase auth"}) == b""


class TestAsASubprocess:
    """Run it exactly as the installer wires it: isolated, bytes over pipes."""

    def run_hook(self, home: Path, payload: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONUTF8", "PYTHONIOENCODING"}
        }
        env["PROJECTMIND_HOME"] = str(home)
        env["PROJECTMIND_EMBEDDING_PROVIDER"] = "hashing"
        return subprocess.run(
            [sys.executable, "-I", "-m", "projectmind.hook_entry", "user-prompt-submit"],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=env,
            cwd=str(home),
            timeout=60,
            check=False,
        )

    def test_non_ascii_prompt_round_trips_with_empty_stderr(self, home: Path) -> None:
        result = self.run_hook(
            home, {"prompt": "Add Supabase auth to a Flutter screen — café", "cwd": "."}
        )
        assert result.returncode == 0
        assert result.stderr == b""
        result.stdout.decode("ascii")
        assert (
            "ProjectMind context"
            in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        )

    def test_garbage_input_exits_zero_and_says_nothing(self, home: Path) -> None:
        env = {**os.environ, "PROJECTMIND_HOME": str(home)}
        result = subprocess.run(
            [sys.executable, "-I", "-m", "projectmind.hook_entry", "user-prompt-submit"],
            input=b"\xff\xfe\x00 not json",
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout == b""
        assert result.stderr == b""


class TestRevive:
    """A daemon the user wants always on is brought back by the next prompt."""

    @pytest.fixture
    def spawned(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        import subprocess

        calls: list[list[str]] = []
        monkeypatch.setattr(subprocess, "Popen", lambda command, **_: calls.append(command))
        return calls

    def test_nothing_is_started_unless_the_user_asked_for_always_on(
        self, tmp_path: Path, spawned: list[list[str]]
    ) -> None:
        assert hook_entry.revive_daemon(tmp_path) is False
        assert spawned == []

    def test_it_is_started_detached_when_always_on(
        self, tmp_path: Path, spawned: list[list[str]]
    ) -> None:
        (tmp_path / hook_entry.AUTOSTART_FILE).write_text("", encoding="utf-8")
        assert hook_entry.revive_daemon(tmp_path) is True
        [command] = spawned
        assert command[-4:] == ["-I", "-m", "projectmind.cli", "serve"]

    def test_a_burst_of_prompts_starts_it_once(
        self, tmp_path: Path, spawned: list[list[str]]
    ) -> None:
        (tmp_path / hook_entry.AUTOSTART_FILE).write_text("", encoding="utf-8")
        assert hook_entry.revive_daemon(tmp_path) is True
        assert hook_entry.revive_daemon(tmp_path) is False
        assert len(spawned) == 1

    def test_a_daemon_that_announced_itself_but_is_gone_is_not_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECTMIND_HOME", str(tmp_path))
        (tmp_path / hook_entry.DAEMON_FILE).write_text(
            '{"port": 9, "token": "x", "pid": 1}', encoding="utf-8"
        )
        assert hook_entry.daemon_alive(timeout=0.3) is False
