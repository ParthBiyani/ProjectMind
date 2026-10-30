from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app, diagnostics
from projectmind.cli.diagnostics import Status
from projectmind.config import get_settings, reset_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PROJECTMIND_HOME", str(tmp_path / "pm"))
    monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
    reset_settings()
    yield
    reset_settings()


def memory_line() -> diagnostics.Finding:
    return next(f for f in diagnostics.check_storage(get_settings()) if f.name == "memory")


class TestProfileCounts:
    def test_statements_awaiting_review_are_counted_and_the_advice_is_to_review(self) -> None:
        runner.invoke(app, ["init"])  # loads the starter statements as proposals
        finding = memory_line()
        assert finding.status is Status.WARN
        assert "0 active" in finding.detail
        assert "50 awaiting your review" in finding.detail
        assert "profile review" in finding.detail

    def test_an_empty_profile_suggests_adding_one(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        finding = memory_line()
        assert "0 active" in finding.detail
        assert "0 awaiting" in finding.detail
        assert "profile add" in finding.detail

    def test_an_active_statement_makes_it_healthy(self) -> None:
        runner.invoke(app, ["init", "--no-seed"])
        runner.invoke(app, ["profile", "add", "Uses Polars instead of pandas for big tables."])
        finding = memory_line()
        assert finding.status is Status.OK
        assert "1 active" in finding.detail
