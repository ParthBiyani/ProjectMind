from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app
from projectmind.config import Settings, get_settings, reset_settings
from projectmind.sources.config import RootConfig, SourcesConfig
from projectmind.storage import SqliteStore, Store
from projectmind.sync import STATE_FILE, resolve_authors, sync

runner = CliRunner()


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "me@example.invalid")
    git(path, "config", "user.name", "Me")
    git(path, "config", "commit.gpgsign", "false")


def commit(path: Path, name: str, message: str) -> None:
    (path / name).write_text(f"# {name}\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)


def age(path: Path, seconds: float = 7_200.0) -> None:
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Store, Settings, Path]]:
    home = tmp_path / "pm"
    home.mkdir()
    root = tmp_path / "Projects"
    repo = root / "alpha"
    make_repo(repo)
    commit(
        repo,
        "a.py",
        "feat: adopt walk-forward validation\n\nBecause k-fold leaked the future into training.",
    )
    notes = repo / "NOTES.md"
    notes.write_text(
        "# Alpha\n\nWe retired the profit factor column for clarity.\n", encoding="utf-8"
    )
    age(notes)
    SourcesConfig(roots=[RootConfig(path=root, kind="project", depth=1)]).save(home)
    settings = Settings(home=home, db_url=None, authors=("me@example.invalid",))
    store = SqliteStore(home / "pm.db")
    store.migrate()
    yield store, settings, root
    store.close()


class TestSync:
    def test_one_pass_reads_history_and_files(self, world: tuple[Store, Settings, Path]) -> None:
        store, settings, _ = world
        report = sync(store, settings)
        assert report.records_stored >= 1
        assert report.files_indexed >= 1
        assert store.count_records() >= 1
        assert store.search_chunks(["profit", "factor"])

    def test_a_second_pass_with_nothing_new_changes_nothing(
        self, world: tuple[Store, Settings, Path]
    ) -> None:
        store, settings, _ = world
        sync(store, settings)
        again = sync(store, settings)
        assert again.records_stored == 0
        assert again.files_indexed == 0

    def test_new_commits_and_edited_files_are_picked_up(
        self, world: tuple[Store, Settings, Path]
    ) -> None:
        store, settings, root = world
        sync(store, settings)
        repo = root / "alpha"
        commit(repo, "b.py", "feat: add the oscillator bars\n\nBecause family C needs them.")
        notes = repo / "NOTES.md"
        notes.write_text(
            "# Alpha\n\nAdded a transformer decoder section for round two.\n", encoding="utf-8"
        )
        age(notes)
        report = sync(store, settings)
        assert report.records_stored >= 1
        assert report.files_indexed >= 1
        assert store.search_chunks(["transformer"])

    def test_git_and_files_can_be_run_separately(self, world: tuple[Store, Settings, Path]) -> None:
        store, settings, _ = world
        only_files = sync(store, settings, git=False)
        assert only_files.git == []
        assert store.count_records() == 0
        assert only_files.files_indexed >= 1

    def test_the_pass_is_remembered_so_the_next_one_reads_less(
        self, world: tuple[Store, Settings, Path]
    ) -> None:
        _, settings, _ = world
        store = SqliteStore(settings.home / "pm2.db")
        store.migrate()
        sync(store, settings)
        assert (settings.home / STATE_FILE).is_file()
        store.close()

    def test_a_missing_folder_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        home = tmp_path / "pm"
        home.mkdir()
        SourcesConfig(roots=[RootConfig(path=tmp_path / "unplugged", depth=1)]).save(home)
        store = SqliteStore(":memory:")
        store.migrate()
        report = sync(store, Settings(home=home, db_url=None))
        assert report.git == []
        assert report.scans[0].error


class TestAuthors:
    def test_configured_authors_are_used(self) -> None:
        settings = Settings(db_url=None, authors=("Old@Example.invalid",))
        assert "old@example.invalid" in resolve_authors(settings)

    def test_authors_can_be_a_comma_separated_environment_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECTMIND_AUTHORS", "a@x.invalid, b@x.invalid")
        assert Settings(db_url=None).authors == ("a@x.invalid", "b@x.invalid")


class TestSyncCommand:
    @pytest.fixture(autouse=True)
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        path = tmp_path / "pmhome"
        monkeypatch.setenv("PROJECTMIND_HOME", str(path))
        monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
        reset_settings()
        runner.invoke(app, ["init", "--no-seed"])
        yield path
        reset_settings()

    def test_the_command_reports_what_it_did(self, tmp_path: Path) -> None:
        root = tmp_path / "Projects"
        notes = root / "beta" / "NOTES.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("# Beta\n\nRetry loop uses exponential backoff.\n", encoding="utf-8")
        age(notes)
        runner.invoke(app, ["sources", "add", str(root)])
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.stdout
        assert "1 files read" in result.stdout
        assert get_settings().home.exists()

    def test_with_no_sources_it_is_a_clean_no_op(self) -> None:
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "0 files read" in result.stdout
