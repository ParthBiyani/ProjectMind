"""The demo is the showcase, so it is a test: if it stops working, a talk breaks."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from projectmind.cli import app
from projectmind.config import reset_settings
from projectmind.demo import STEPS, StepResult, demo_memory, run_demo

runner = CliRunner()


@pytest.fixture(scope="module")
def results() -> Iterator[list[StepResult]]:
    with demo_memory() as (memory, paths):
        yield run_demo(memory, paths)


def by_title(results: list[StepResult], title: str) -> StepResult:
    return next(result for result in results if result.step.title == title)


class TestTour:
    def test_there_are_seven_steps_and_two_end_in_silence_or_skip(
        self, results: list[StepResult]
    ) -> None:
        assert len(results) == len(STEPS) == 7
        assert sum(1 for r in results if r.silent) == 2

    def test_a_mechanical_edit_is_skipped(self, results: list[StepResult]) -> None:
        step = by_title(results, "A mechanical edit")
        assert step.skipped
        assert step.silent

    def test_the_notes_step_cites_the_file_and_lines(self, results: list[StepResult]) -> None:
        step = by_title(results, "Your own notes, cited")
        assert step.citations
        assert re.search(r"CLAUDE\.md:\d+-\d+$", step.citations[0])
        assert "0.55*Dice" in "\n".join(step.excerpts)

    def test_git_history_answers_a_question_about_your_own_project(
        self, results: list[StepResult]
    ) -> None:
        step = by_title(results, "Your own git history")
        assert any("/commit/" in c or c.startswith("git://") for c in step.citations)
        assert "profit factor" in "\n".join(step.excerpts).lower()

    def test_knowledge_crosses_from_another_project(self, results: list[StepResult]) -> None:
        step = by_title(results, "Knowledge from another project")
        assert any("echo-notes" in c and "DECISIONS.md" in c for c in step.citations)
        assert "jitter" in "\n".join(step.excerpts)

    def test_a_past_session_is_cited_by_turn(self, results: list[StepResult]) -> None:
        step = by_title(results, "What was decided in a session")
        assert any("turn 1" in c for c in step.citations)
        assert "FTS5" in "\n".join(step.excerpts)

    def test_a_captured_page_is_served_with_its_url(self, results: list[StepResult]) -> None:
        step = by_title(results, "What you read")
        assert any("postgresql.org/docs/current/textsearch-rank.html" in c for c in step.citations)
        assert not any("?x=1" in c for c in step.citations)

    def test_an_absent_topic_returns_nothing(self, results: list[StepResult]) -> None:
        assert by_title(results, "Nothing to say").silent

    def test_it_is_fast_and_small(self, results: list[StepResult]) -> None:
        assert max(r.tokens for r in results) <= 2300
        assert max(r.latency_ms for r in results) < 2000

    def test_the_demo_never_reads_the_real_browser_history(self) -> None:
        """It once did, through sync: the throwaway store held pages from the real browser."""
        with demo_memory() as (memory, _):
            web = [d for d in memory.store.list_documents(limit=1000) if d.source_type == "web"]
        assert [d.uri for d in web] == [
            "https://www.postgresql.org/docs/current/textsearch-rank.html"
        ]

    def test_nothing_in_the_workspace_leaks_into_the_real_home(self, tmp_path: Path) -> None:
        with demo_memory() as (memory, _):
            assert memory.settings.home != Path.home() / ".projectmind"


class TestCommand:
    @pytest.fixture(autouse=True)
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setenv("PROJECTMIND_HOME", str(tmp_path / "untouched"))
        reset_settings()
        yield
        reset_settings()

    def test_the_tour_runs_end_to_end_and_leaves_the_real_home_alone(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["demo"])
        assert result.exit_code == 0, result.stdout
        assert "5 of 7 questions were answered" in result.stdout
        assert "Rename this variable" in result.stdout
        assert not (tmp_path / "untouched" / "projectmind.db").exists()

    def test_real_mode_needs_a_question(self) -> None:
        assert runner.invoke(app, ["demo", "--real"]).exit_code == 1
