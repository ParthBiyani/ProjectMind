from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app, diagnostics
from projectmind.cli.diagnostics import Status
from projectmind.config import get_settings, reset_settings

runner = CliRunner()

EXPECTED_COMMANDS = {
    "init",
    "context",
    "report",
    "doctor",
    "mcp",
    "export",
    "profile",
    "install",
    "hook",
    "history",
    "fingerprint",
    "eval",
    "ingest",
    "review",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway ProjectMind home and a throwaway Claude config directory."""
    monkeypatch.setenv("PROJECTMIND_HOME", str(tmp_path / "pm"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
    reset_settings()
    runner.invoke(app, ["init", "--no-seed"])
    yield tmp_path / "claude"
    reset_settings()


def by_name(findings: list[diagnostics.Finding]) -> dict[str, diagnostics.Finding]:
    return {finding.name: finding for finding in findings}


class TestCliSurface:
    def test_every_documented_command_is_registered(self) -> None:
        """Guards against a refactor quietly dropping a command."""
        registered = {
            command.name or getattr(command.callback, "__name__", "")
            for command in app.registered_commands
        } | {group.name for group in app.registered_groups}
        assert registered >= EXPECTED_COMMANDS


class TestHookChecks:
    def test_nothing_installed_is_a_warning_not_a_pass(self) -> None:
        found = by_name(diagnostics.check_hooks())
        assert found["claude hooks"].status is Status.WARN

    def test_installed_hooks_are_reported_installed(self, isolated: Path) -> None:
        runner.invoke(app, ["install", "claude-code"])
        found = by_name(diagnostics.check_hooks())
        assert found["hook UserPromptSubmit"].status is Status.OK
        assert found["hook SessionStart"].status is Status.OK
        assert diagnostics.check_mcp_registration().status is Status.OK

    def test_the_mcp_entry_lands_beside_the_hooks(self, isolated: Path) -> None:
        runner.invoke(app, ["install", "claude-code"])
        assert (isolated / "settings.json").is_file()
        assert (isolated / ".claude.json").is_file()

    def test_a_hook_pointing_at_a_missing_interpreter_fails(self, isolated: Path) -> None:
        isolated.mkdir(parents=True)
        stale = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"C:\gone\python.exe" -I -m projectmind.hook_entry '
                                "user-prompt-submit",
                            }
                        ],
                    }
                ]
            }
        }
        (isolated / "settings.json").write_text(json.dumps(stale), encoding="utf-8")
        found = by_name(diagnostics.check_hooks())
        assert found["hook UserPromptSubmit"].status is Status.FAIL
        assert "no longer exists" in found["hook UserPromptSubmit"].detail

    def test_another_tools_hook_is_not_mistaken_for_ours(self, isolated: Path) -> None:
        """A hook whose path merely contains the word is not ProjectMind's."""
        isolated.mkdir(parents=True)
        other = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "node D:/Projects/ProjectMind/x.js"}]}
                ]
            }
        }
        (isolated / "settings.json").write_text(json.dumps(other), encoding="utf-8")
        found = by_name(diagnostics.check_hooks())
        assert found["hook UserPromptSubmit"].status is Status.WARN

    def test_uninstall_leaves_other_tools_hooks_alone(self, isolated: Path) -> None:
        isolated.mkdir(parents=True)
        theirs = {"hooks": [{"type": "command", "command": "node D:/Projects/ProjectMind/x.js"}]}
        (isolated / "settings.json").write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [theirs]}}), encoding="utf-8"
        )
        runner.invoke(app, ["install", "claude-code"])
        runner.invoke(app, ["install", "claude-code", "--uninstall"])
        remaining = json.loads((isolated / "settings.json").read_text(encoding="utf-8"))
        assert remaining["hooks"]["UserPromptSubmit"] == [theirs]


class TestSelfTest:
    def test_the_hook_really_runs_and_is_judged_on_its_output(self) -> None:
        finding = diagnostics.check_hook_runs(get_settings())
        assert finding.status in {Status.OK, Status.WARN}, finding.detail
        assert "ms" in finding.detail

    def test_the_self_test_does_not_pollute_the_history(self) -> None:
        from projectmind.storage import HistoryFilter, open_store

        diagnostics.check_hook_runs(get_settings())
        store = open_store(get_settings())
        try:
            assert store.find_bundles(HistoryFilter()) == []
        finally:
            store.close()


class TestDoctorCommand:
    def test_doctor_reports_missing_wiring_but_does_not_fail(self) -> None:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0, result.stdout
        assert "hook self-test" in result.stdout
        assert "claude hooks" in result.stdout
        assert "need attention" in result.stdout

    def test_doctor_exits_nonzero_when_something_is_broken(
        self, isolated: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            diagnostics,
            "check_git",
            lambda: diagnostics.Finding("git", Status.FAIL, "not found"),
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
