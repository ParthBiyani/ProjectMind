from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app
from projectmind.config import get_settings, reset_settings
from projectmind.models import Category, StatementStatus
from projectmind.profile.repository import ProfileRepository
from projectmind.storage import open_store

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


def statements() -> list:  # type: ignore[type-arg]
    store = open_store(get_settings())
    try:
        return ProfileRepository(store, get_settings()).all()
    finally:
        store.close()


class TestAdd:
    def test_a_statement_you_write_is_active_at_once(self) -> None:
        result = runner.invoke(
            app,
            [
                "profile",
                "add",
                "Uses Polars instead of pandas for anything over a gigabyte.",
                "--category",
                "tool_preference",
                "--entity",
                "polars",
                "--entity",
                "pandas",
            ],
        )
        assert result.exit_code == 0, result.stdout
        [statement] = statements()
        assert statement.status is StatementStatus.ACTIVE
        assert statement.category is Category.TOOL_PREFERENCE
        assert statement.confidence >= 0.85
        assert set(statement.entities) == {"polars", "pandas"}

    def test_it_is_actually_served(self) -> None:
        runner.invoke(
            app,
            [
                "profile",
                "add",
                "Uses Polars instead of pandas for anything over a gigabyte.",
                "-c",
                "tool_preference",
                "-e",
                "polars",
            ],
        )
        result = runner.invoke(
            app, ["context", "Load the big polars dataframe and group it", "--raw"]
        )
        assert "Polars" in result.stdout

    def test_a_non_statement_is_refused(self) -> None:
        result = runner.invoke(app, ["profile", "add", "polars"])
        assert result.exit_code == 1
        assert statements() == []

    def test_known_entities_in_the_text_are_picked_up(self) -> None:
        runner.invoke(app, ["profile", "add", "Prefers FastAPI over Flask.", "-e", "fastapi"])
        runner.invoke(app, ["profile", "add", "Deploys the fastapi services on small VMs."])
        second = next(s for s in statements() if "VMs" in s.statement)
        assert "fastapi" in second.entities


class TestEdit:
    def add_one(self) -> str:
        runner.invoke(app, ["profile", "add", "Uses pandas for tabular work everywhere."])
        return str(statements()[0].id)

    def test_new_wording_replaces_the_old(self) -> None:
        identifier = self.add_one()
        result = runner.invoke(
            app, ["profile", "edit", identifier[:8], "--text", "Uses polars for tabular work."]
        )
        assert result.exit_code == 0, result.stdout
        [statement] = statements()
        assert statement.statement == "Uses polars for tabular work."
        assert str(statement.id) == identifier

    def test_editing_counts_as_confirming_today(self) -> None:
        identifier = self.add_one()
        before = statements()[0].last_confirmed_at
        runner.invoke(app, ["profile", "edit", identifier[:8], "-c", "constraint"])
        after = statements()[0]
        assert after.category is Category.CONSTRAINT
        assert after.last_confirmed_at >= before

    def test_an_unknown_id_is_a_clean_error(self) -> None:
        result = runner.invoke(app, ["profile", "edit", "deadbeef", "--text", "Something long."])
        assert result.exit_code == 1


class TestRemove:
    def test_a_removed_statement_is_kept_but_never_served(self) -> None:
        runner.invoke(app, ["profile", "add", "Uses pandas for tabular work everywhere."])
        identifier = str(statements()[0].id)
        result = runner.invoke(app, ["profile", "remove", identifier[:8], "--yes"])
        assert result.exit_code == 0
        [statement] = statements()
        assert statement.status is StatementStatus.REJECTED
        served = runner.invoke(app, ["context", "Clean up the pandas tabular code", "--raw"])
        assert "pandas" not in served.stdout


class TestSeed:
    def test_every_seed_statement_can_be_approved_through_review(self) -> None:
        """One seed statement used to sit at 0.84, under the 0.85 activation bar,
        so approving it in `profile review` failed."""
        runner.invoke(app, ["profile", "seed"])
        proposed = [s for s in statements() if s.status is StatementStatus.PROPOSED]
        assert proposed
        assert all(s.confidence >= get_settings().profile_activation_confidence for s in proposed)
