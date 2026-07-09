from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app, install_cmd
from projectmind.config import reset_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every command at a throwaway home, never the real one."""
    home = tmp_path / "pmhome"
    monkeypatch.setenv("PROJECTMIND_HOME", str(home))
    monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
    reset_settings()
    yield home
    reset_settings()


def invoke(*args: str, stdin: str | None = None) -> object:
    return runner.invoke(app, list(args), input=stdin)


class TestTopLevel:
    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "projectmind" in result.stdout

    def test_bare_invocation_prints_help_rather_than_failing(self) -> None:
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "Cross-project engineering memory" in result.stdout


class TestInit:
    def test_init_creates_the_home_and_seeds_proposals(self, isolated_home: Path) -> None:
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.stdout
        assert (isolated_home / "projectmind.db").exists()
        assert "50 loaded" in result.stdout

    def test_init_is_idempotent(self) -> None:
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "50 already present" in result.stdout

    def test_init_can_skip_seeding(self) -> None:
        result = runner.invoke(app, ["init", "--no-seed"])
        assert result.exit_code == 0
        assert "loaded" not in result.stdout


class TestProfileCommands:
    def test_list_is_empty_before_init(self) -> None:
        result = runner.invoke(app, ["profile", "list"])
        assert result.exit_code == 0
        assert "no statements match" in result.stdout

    def test_stats_after_seeding(self) -> None:
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["profile", "stats"])
        assert result.exit_code == 0
        assert "statements" in result.stdout
        assert "50" in result.stdout

    def test_review_approves_what_you_approve(self) -> None:
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["profile", "review", "--limit", "3"], input="a\nd\ns\n")
        assert result.exit_code == 0
        assert "1 approved" in result.stdout
        listed = runner.invoke(app, ["profile", "list", "--status", "active"])
        assert "1 statements" in listed.stdout

    def test_review_says_so_when_there_is_nothing_to_do(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        result = runner.invoke(app, ["profile", "review"])
        assert "nothing to review" in result.stdout

    def test_sweep_reports_what_it_examined(self) -> None:
        runner.invoke(app, ["init"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        result = runner.invoke(app, ["profile", "sweep"])
        assert result.exit_code == 0
        assert "examined" in result.stdout

    def test_show_rejects_a_malformed_id(self) -> None:
        result = runner.invoke(app, ["profile", "show", "not-a-uuid"])
        assert result.exit_code == 1


class TestContext:
    def test_nothing_is_injected_before_anything_is_approved(self) -> None:
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["context", "Add Supabase auth", "--project", "."])
        assert result.exit_code == 0
        assert "nothing injected" in result.stdout

    def test_an_approved_profile_is_injected(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        result = runner.invoke(app, ["context", "Add Supabase auth", "--project", "."])
        assert result.exit_code == 0
        assert "ProjectMind context" in result.stdout

    def test_json_output_round_trips(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        result = runner.invoke(app, ["context", "Add Supabase auth", "--json", "--project", "."])
        payload = json.loads(result.stdout)
        assert payload["profile"]
        assert payload["profile_tokens"] > 0

    def test_raw_output_is_exactly_what_would_be_injected(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        result = runner.invoke(app, ["context", "Add Supabase auth", "--raw", "--project", "."])
        assert result.stdout.startswith("## ProjectMind context")
        assert "tokens" not in result.stdout.splitlines()[0]

    def test_quiet_does_not_pollute_the_bundle_log(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        runner.invoke(app, ["context", "Add Supabase auth", "--quiet", "--project", "."])
        result = runner.invoke(app, ["report"])
        assert "no context bundles" in result.stdout


class TestReportAndDoctor:
    def test_report_is_quiet_when_there_is_nothing_to_report(self) -> None:
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["report"])
        assert "no context bundles" in result.stdout

    def test_report_counts_calls(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        runner.invoke(app, ["context", "Add Supabase auth", "--project", "."])
        result = runner.invoke(app, ["report"])
        assert "context calls" in result.stdout
        assert "100.0%" in result.stdout

    def test_doctor_passes_on_a_working_install(self) -> None:
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "storage" in result.stdout
        assert "embeddings" in result.stdout

    def test_export_writes_every_statement(self, tmp_path: Path) -> None:
        runner.invoke(app, ["init"])
        destination = tmp_path / "profile.json"
        result = runner.invoke(app, ["export", str(destination)])
        assert result.exit_code == 0
        assert len(json.loads(destination.read_text(encoding="utf-8"))) == 50


class TestHook:
    def test_the_hook_prints_context_for_a_real_prompt(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "seed", "--activate-all"])
        payload = json.dumps({"prompt": "Add Supabase auth to a Flutter screen", "cwd": "."})
        result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
        assert result.exit_code == 0
        assert "ProjectMind context" in result.stdout

    def test_the_hook_prints_nothing_when_memory_is_empty(self) -> None:
        runner.invoke(app, ["init"])
        payload = json.dumps({"prompt": "Add Supabase auth", "cwd": "."})
        result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
        assert result.exit_code == 0
        assert result.stdout.strip() == ""

    def test_an_empty_prompt_is_a_silent_no_op(self) -> None:
        result = runner.invoke(app, ["hook", "user-prompt-submit"], input='{"prompt": ""}')
        assert result.exit_code == 0
        assert result.stdout.strip() == ""

    def test_garbage_on_stdin_never_breaks_the_session(self) -> None:
        result = runner.invoke(app, ["hook", "user-prompt-submit"], input="not json at all")
        assert result.exit_code == 0
        assert result.stdout.strip() == ""

    def test_session_start_is_silent(self) -> None:
        runner.invoke(app, ["init"])
        payload = json.dumps({"cwd": "."})
        result = runner.invoke(app, ["hook", "session-start"], input=payload)
        assert result.exit_code == 0
        assert result.stdout.strip() == ""


class TestInstaller:
    @pytest.fixture(autouse=True)
    def redirect_agent_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[tuple[Path, Path]]:
        claude_dir = tmp_path / "claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        mcp_path = tmp_path / ".claude.json"
        monkeypatch.setattr(install_cmd, "_claude_dir", lambda: claude_dir)
        monkeypatch.setattr(install_cmd, "_mcp_config_path", lambda: mcp_path)
        yield settings_path, mcp_path

    def test_dry_run_writes_nothing(self, redirect_agent_config: tuple[Path, Path]) -> None:
        settings_path, mcp_path = redirect_agent_config
        result = runner.invoke(app, ["install", "claude-code", "--dry-run"])
        assert result.exit_code == 0
        assert not settings_path.exists()
        assert not mcp_path.exists()

    def test_install_adds_hooks_and_the_server(
        self, redirect_agent_config: tuple[Path, Path]
    ) -> None:
        settings_path, mcp_path = redirect_agent_config
        result = runner.invoke(app, ["install", "claude-code"])
        assert result.exit_code == 0
        hooks = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]
        assert "projectmind" in json.dumps(hooks["UserPromptSubmit"])
        assert "projectmind" in json.dumps(hooks["SessionStart"])
        servers = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
        assert servers["projectmind"]["args"] == ["-m", "projectmind.cli", "mcp"]

    def test_existing_hooks_from_other_tools_are_preserved(
        self, redirect_agent_config: tuple[Path, Path]
    ) -> None:
        """The machine this runs on already has another tool in these hooks."""
        settings_path, _ = redirect_agent_config
        settings_path.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "matcher": "",
                                "hooks": [{"type": "command", "command": "python other_tool.py"}],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        runner.invoke(app, ["install", "claude-code"])
        config = json.loads(settings_path.read_text(encoding="utf-8"))
        entries = config["hooks"]["UserPromptSubmit"]
        assert len(entries) == 2
        assert "other_tool.py" in json.dumps(entries)
        assert config["model"] == "opus", "unrelated settings must survive"

    def test_installing_twice_does_not_duplicate(
        self, redirect_agent_config: tuple[Path, Path]
    ) -> None:
        settings_path, _ = redirect_agent_config
        runner.invoke(app, ["install", "claude-code"])
        result = runner.invoke(app, ["install", "claude-code"])
        assert "already present" in result.stdout
        entries = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
        assert len(entries) == 1

    def test_a_backup_is_written_before_any_change(
        self, redirect_agent_config: tuple[Path, Path]
    ) -> None:
        settings_path, _ = redirect_agent_config
        settings_path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        runner.invoke(app, ["install", "claude-code"])
        backups = list(settings_path.parent.glob("settings.json.bak-projectmind-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == {"model": "opus"}

    def test_uninstall_removes_only_what_was_installed(
        self, redirect_agent_config: tuple[Path, Path]
    ) -> None:
        settings_path, mcp_path = redirect_agent_config
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {"matcher": "", "hooks": [{"command": "python other_tool.py"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        runner.invoke(app, ["install", "claude-code"])
        runner.invoke(app, ["install", "claude-code", "--uninstall"])
        config = json.loads(settings_path.read_text(encoding="utf-8"))
        entries = config["hooks"]["UserPromptSubmit"]
        assert len(entries) == 1
        assert "other_tool.py" in json.dumps(entries)
        assert "projectmind" not in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]

    def test_status_reports_both_states(self, redirect_agent_config: tuple[Path, Path]) -> None:
        before = runner.invoke(app, ["install", "status"])
        assert before.stdout.count("off") == 3
        runner.invoke(app, ["install", "claude-code"])
        after = runner.invoke(app, ["install", "status"])
        assert "off" not in after.stdout
        assert after.stdout.count("on") >= 3

    def test_rules_snippet_is_appended_once(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "CLAUDE.md"
        rules_file.write_text("# Project rules\n", encoding="utf-8")
        runner.invoke(app, ["install", "rules", "--write", str(rules_file)])
        runner.invoke(app, ["install", "rules", "--write", str(rules_file)])
        content = rules_file.read_text(encoding="utf-8")
        assert content.count("## ProjectMind") == 1
        assert content.startswith("# Project rules")

    def test_malformed_agent_config_is_reported_rather_than_overwritten(
        self, redirect_agent_config: tuple[Path, Path]
    ) -> None:
        settings_path, _ = redirect_agent_config
        settings_path.write_text("{ not json", encoding="utf-8")
        result = runner.invoke(app, ["install", "claude-code"])
        assert result.exit_code != 0
        assert settings_path.read_text(encoding="utf-8") == "{ not json"
