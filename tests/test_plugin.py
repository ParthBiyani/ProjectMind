"""The Claude Code plugin that lets anyone say "install ProjectMind".

These tests keep the marketplace, the plugin manifest and the skill in step with each other and
with the package, because a mismatch only shows up when a stranger tries to install it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from projectmind import __version__

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "projectmind"
SKILL = PLUGIN / "skills" / "install"


def load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def frontmatter() -> dict[str, str]:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n", 2)[1])  # type: ignore[no-any-return]


class TestManifests:
    def test_the_marketplace_points_at_the_plugin_folder(self) -> None:
        market = load(MARKETPLACE)
        [entry] = market["plugins"]  # type: ignore[misc]
        assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()
        assert market["owner"] == {"name": "Parth Biyani"}

    def test_the_names_agree_so_the_install_id_resolves(self) -> None:
        [entry] = load(MARKETPLACE)["plugins"]  # type: ignore[misc]
        assert entry["name"] == load(PLUGIN / ".claude-plugin" / "plugin.json")["name"]

    def test_the_plugin_version_is_the_package_version(self) -> None:
        assert load(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == __version__

    def test_nobody_but_the_author_is_listed_as_author_or_contributor(self) -> None:
        manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
        market = load(MARKETPLACE)
        assert manifest["author"] == {"name": "Parth Biyani"}
        assert market["owner"] == {"name": "Parth Biyani"}
        for data in (manifest, market):
            assert "contributors" not in data
        for path in (PLUGIN / ".claude-plugin" / "plugin.json", MARKETPLACE):
            assert not re.search(
                r"co-?authored?|contributor", path.read_text(encoding="utf-8"), re.I
            )


class TestSkill:
    def test_it_is_named_install_so_the_command_is_projectmind_install(self) -> None:
        assert frontmatter()["name"] == "install"

    def test_the_description_says_what_it_does_and_when_to_use_it(self) -> None:
        meta = frontmatter()
        assert "ProjectMind" in meta["description"]
        assert len(meta["description"]) + len(meta["when_to_use"]) < 1536
        for phrase in ("install ProjectMind", "set up ProjectMind"):
            assert phrase in meta["when_to_use"]

    def test_every_command_it_tells_the_agent_to_run_exists(self) -> None:
        from typer.testing import CliRunner

        from projectmind.cli import app

        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        runner = CliRunner()
        commands = {
            tuple(line.split()[1:3])
            for line in re.findall(r"^projectmind [a-z]+(?: [a-z-]+)?", body, re.MULTILINE)
        }
        assert commands
        for first, *rest in (tuple(c) for c in commands):
            result = runner.invoke(app, [first, *rest, "--help"])
            if result.exit_code != 0:
                result = runner.invoke(app, [first, "--help"])  # the second word was an argument
            assert result.exit_code == 0, (first, rest, result.output)

    def test_it_asks_before_reading_files_or_changing_configuration(self) -> None:
        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        assert body.count("(ASK") >= 4
        assert "--no-seed" in body
        assert "Look before you index" in body

    def test_it_refers_to_its_script_by_the_documented_variable(self) -> None:
        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        assert "${CLAUDE_SKILL_DIR}/scripts/preflight.py" in body
        assert (SKILL / "scripts" / "preflight.py").is_file()


class TestPreflight:
    def run(self) -> dict[str, object]:
        done = subprocess.run(
            [sys.executable, str(SKILL / "scripts" / "preflight.py")],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return json.loads(done.stdout)  # type: ignore[no-any-return]

    def test_it_reports_the_machine_as_json(self) -> None:
        report = self.run()
        assert report["python"]["ok"] is True  # type: ignore[index]
        assert set(report["capabilities"]) == {  # type: ignore[call-overload]
            "core (index, hook, MCP, daemon)",
            "start at logon",
            "browser history and page capture",
        }

    @pytest.mark.skipif(sys.platform != "win32", reason="logon start and browser are Windows-only")
    def test_it_says_the_windows_only_features_are_available_on_windows(self) -> None:
        report = self.run()
        assert report["capabilities"]["start at logon"] is True  # type: ignore[index]

    def test_it_reads_no_user_files(self) -> None:
        source = (SKILL / "scripts" / "preflight.py").read_text(encoding="utf-8")
        assert "read_text" not in source
        assert "open(" not in source
