from __future__ import annotations

from pathlib import Path

import pytest

from projectmind.config import env_file, get_settings, reset_settings
from projectmind.evaluation.paths import ENV_VAR, eval_dir


@pytest.fixture(autouse=True)
def fresh_settings() -> None:
    reset_settings()
    yield  # type: ignore[misc]
    reset_settings()


class TestEnvFile:
    def test_the_env_file_lives_in_the_projectmind_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECTMIND_HOME", str(tmp_path / "pm"))
        assert env_file() == tmp_path / "pm" / ".env"

    def test_values_in_the_home_env_file_are_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "pm"
        home.mkdir()
        (home / ".env").write_text("PROJECTMIND_HISTORY=minimal\n", encoding="utf-8")
        monkeypatch.setenv("PROJECTMIND_HOME", str(home))
        monkeypatch.delenv("PROJECTMIND_HISTORY", raising=False)
        assert get_settings().history == "minimal"

    def test_a_dotenv_in_the_working_directory_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook runs inside whatever repository the user is in. That
        repository's `.env` must not be able to reconfigure ProjectMind."""
        project = tmp_path / "some-project"
        project.mkdir()
        (project / ".env").write_text(
            "PROJECTMIND_HISTORY=off\nPROJECTMIND_DB_URL=postgresql://evil/db\n", encoding="utf-8"
        )
        monkeypatch.chdir(project)
        monkeypatch.setenv("PROJECTMIND_HOME", str(tmp_path / "pm"))
        monkeypatch.delenv("PROJECTMIND_HISTORY", raising=False)
        monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
        settings = get_settings()
        assert settings.history == "full"
        assert settings.db_url is None


class TestEvalDir:
    def test_the_environment_variable_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "queries.yaml").write_text("x: 1\n", encoding="utf-8")
        monkeypatch.setenv(ENV_VAR, str(tmp_path))
        assert eval_dir() == tmp_path

    def test_the_checkout_is_found_without_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert (eval_dir() / "queries.yaml").is_file()

    def test_a_missing_set_says_where_it_looked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "nope"))
        monkeypatch.setattr("projectmind.evaluation.paths.Path.cwd", lambda: tmp_path)
        monkeypatch.setattr(
            "projectmind.evaluation.paths.__file__", str(tmp_path / "a" / "b" / "c" / "d.py")
        )
        with pytest.raises(FileNotFoundError, match="PROJECTMIND_EVAL_DIR"):
            eval_dir()


class TestListSettingsFromTheEnvironment:
    """Comma-separated lists are documented for these, and used to be rejected."""

    def test_excluded_repos_accepts_a_comma_separated_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECTMIND_EXCLUDED_REPOS", "client-*, secret-repo")
        from projectmind.config import Settings

        assert Settings(db_url=None).excluded_repos == ("client-*", "secret-repo")

    def test_an_empty_value_means_no_exclusions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROJECTMIND_EXCLUDED_REPOS", "")
        from projectmind.config import Settings

        assert Settings(db_url=None).excluded_repos == ()
