from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app, schedule_cmd

runner = CliRunner()
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows logon scheduling")


class FakeSchtasks:
    """Stand-in for schtasks, so no real task is ever created or deleted."""

    def __init__(self, *, allow_create: bool) -> None:
        self.allow_create = allow_create
        self.registered = False
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        verb = args[1]
        if verb == "/Create":
            if self.allow_create:
                self.registered = True
                return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
            return subprocess.CompletedProcess(args, 1, "", "ERROR: Access is denied.")
        if verb == "/Query":
            return subprocess.CompletedProcess(args, 0 if self.registered else 1, "", "")
        if verb == "/Delete":
            self.registered = False
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    yield schedule_cmd.startup_dir()


class TestInstall:
    def test_task_scheduler_is_used_when_it_allows_it(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeSchtasks(allow_create=True)
        monkeypatch.setattr(subprocess, "run", fake)
        result = runner.invoke(app, ["schedule", "install"])
        assert result.exit_code == 0, result.stdout
        assert fake.registered
        assert not schedule_cmd.launcher_path().exists()

    def test_the_startup_folder_is_the_fallback_when_it_refuses(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", FakeSchtasks(allow_create=False))
        result = runner.invoke(app, ["schedule", "install"])
        assert result.exit_code == 0, result.stdout
        assert schedule_cmd.launcher_path().exists()

    def test_the_launcher_starts_the_daemon_hidden_and_isolated(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", FakeSchtasks(allow_create=False))
        runner.invoke(app, ["schedule", "install"])
        script = schedule_cmd.launcher_path().read_text(encoding="utf-8")
        assert ", 0, False" in script  # window style 0: no console window
        assert "-I -m projectmind.cli serve" in script
        assert "projectmind.cli serve" in schedule_cmd.daemon_command()

    def test_installing_twice_is_harmless(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", FakeSchtasks(allow_create=False))
        runner.invoke(app, ["schedule", "install"])
        again = runner.invoke(app, ["schedule", "install"])
        assert again.exit_code == 0
        assert len(list(startup.glob("*.vbs"))) == 1


class TestRemoveAndStatus:
    def test_remove_takes_away_the_launcher(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", FakeSchtasks(allow_create=False))
        runner.invoke(app, ["schedule", "install"])
        result = runner.invoke(app, ["schedule", "remove"])
        assert result.exit_code == 0
        assert not schedule_cmd.launcher_path().exists()

    def test_remove_takes_away_the_task_too(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeSchtasks(allow_create=True)
        monkeypatch.setattr(subprocess, "run", fake)
        runner.invoke(app, ["schedule", "install"])
        assert runner.invoke(app, ["schedule", "remove"]).exit_code == 0
        assert not fake.registered

    def test_remove_with_nothing_installed_says_so(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", FakeSchtasks(allow_create=False))
        assert runner.invoke(app, ["schedule", "remove"]).exit_code == 1

    def test_status_reports_where_it_is_set_up(
        self, startup: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", FakeSchtasks(allow_create=False))
        assert runner.invoke(app, ["schedule", "status"]).exit_code == 1
        runner.invoke(app, ["schedule", "install"])
        result = runner.invoke(app, ["schedule", "status"])
        assert result.exit_code == 0
        assert "Startup launcher" in result.stdout
