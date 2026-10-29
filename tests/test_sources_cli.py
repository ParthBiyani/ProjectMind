from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app
from projectmind.config import reset_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "pmhome"
    monkeypatch.setenv("PROJECTMIND_HOME", str(path))
    monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
    reset_settings()
    runner.invoke(app, ["init", "--no-seed"])
    yield path
    reset_settings()


def make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "Projects"
    for name, text in {
        "alpha": "# Alpha\n\nWe dropped the profit factor column because it confused readers.\n",
        "beta": "# Beta\n\nRetry loop uses exponential backoff with jitter on rate limits.\n",
    }.items():
        file = root / name / "NOTES.md"
        file.parent.mkdir(parents=True)
        file.write_text(text, encoding="utf-8")
        stamp = file.stat().st_mtime - 7_200
        os.utime(file, (stamp, stamp))
    return root


class TestSourcesCommands:
    def test_no_sources_is_a_hint_not_an_error(self) -> None:
        result = runner.invoke(app, ["sources", "list"])
        assert result.exit_code == 0
        assert "sources add" in result.stdout

    def test_add_scan_list_and_search(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        assert runner.invoke(app, ["sources", "add", str(root)]).exit_code == 0
        scanned = runner.invoke(app, ["sources", "scan"])
        assert scanned.exit_code == 0, scanned.stdout
        assert "2 files read" in scanned.stdout

        listed = runner.invoke(app, ["sources", "list"])
        assert "2 chunks indexed" in listed.stdout

        found = runner.invoke(app, ["sources", "search", "profit factor"])
        assert "alpha" in found.stdout
        assert "NOTES.md" in found.stdout
        assert "profit factor column" in found.stdout

    def test_search_finds_nothing_for_what_is_not_there(self, tmp_path: Path) -> None:
        runner.invoke(app, ["sources", "add", str(make_tree(tmp_path))])
        runner.invoke(app, ["sources", "scan"])
        result = runner.invoke(app, ["sources", "search", "kubernetes"])
        assert "nothing indexed says that" in result.stdout

    def test_removing_a_source_forgets_its_documents(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        runner.invoke(app, ["sources", "add", str(root)])
        runner.invoke(app, ["sources", "scan"])
        removed = runner.invoke(app, ["sources", "remove", str(root)])
        assert "forgot 2 documents" in removed.stdout
        assert "nothing indexed" in runner.invoke(app, ["sources", "search", "backoff"]).stdout

    def test_a_missing_folder_is_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["sources", "add", str(tmp_path / "nope")])
        assert result.exit_code == 1

    def test_a_folder_that_is_not_configured_cannot_be_scanned_alone(self, tmp_path: Path) -> None:
        runner.invoke(app, ["sources", "add", str(make_tree(tmp_path))])
        result = runner.invoke(app, ["sources", "scan", str(tmp_path / "elsewhere")])
        assert result.exit_code == 1
