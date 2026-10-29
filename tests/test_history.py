from __future__ import annotations

import functools
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from projectmind import hook_entry
from projectmind.cli import app
from projectmind.config import Settings, get_settings, reset_settings
from projectmind.evaluation import EvalSet
from projectmind.evaluation.fixtures import FixtureCorpus
from projectmind.evaluation.live import load_corpus
from projectmind.service import MemoryService
from projectmind.storage import HistoryFilter, SqliteStore, open_store

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]


#: Recency must not decide whether these tests have anything to retrieve: the
#: fixture records are dated, and the wall clock keeps moving.
NO_DECAY = 1_000_000.0


@functools.cache
def answerable_query() -> tuple[str, str]:
    """A fixture query that really does inject episodic memory."""
    eval_set = EvalSet.from_yaml(REPO_ROOT / "eval" / "queries.yaml")
    with tempfile.TemporaryDirectory() as tmp, service_with(Path(tmp)) as memory:
        for query in eval_set.queries:
            if not query.relevant:
                continue
            if memory.get_context(query.prompt, query.project, log_bundle=False).episodic:
                return query.prompt, query.project
    raise AssertionError("no fixture query injects episodic memory")


def service_with(tmp_path: Path, **settings: object) -> MemoryService:
    settings.setdefault("recency_half_life_days", NO_DECAY)
    config = Settings(home=tmp_path, db_url=None, **settings)  # type: ignore[arg-type]
    store = SqliteStore(tmp_path / "history.db")
    store.migrate()
    load_corpus(store, FixtureCorpus.load(REPO_ROOT / "eval" / "fixtures" / "corpus.yaml"))
    return MemoryService(store, config)


class TestHistoryModes:
    def test_full_keeps_the_question_the_answer_and_the_trace(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path) as memory:
            bundle = memory.get_context(prompt, project, source="hook", session_id="s-1")
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        assert entry.prompt_text == prompt
        assert entry.source == "hook"
        assert entry.session_id == "s-1"
        assert entry.rendered_text == bundle.render()
        assert entry.trace["decision"]["reason"]
        assert entry.embedder == "hashing"
        assert entry.version

    def test_every_served_record_appears_in_the_trace_as_served(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path) as memory:
            bundle = memory.get_context(prompt, project)
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        served = {
            row["record"]
            for row in entry.trace.get("retrieval", [])
            if row.get("outcome") == "served"
        }
        assert served == {str(item.id) for item in bundle.episodic}
        assert bundle.episodic, "the chosen query should inject episodic memory"

    def test_the_trace_explains_what_was_cut_and_why(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path) as memory:
            bundle = memory.get_context(prompt, project)
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        outcomes = {row.get("outcome") for row in entry.trace["retrieval"] if "outcome" in row}
        assert "served" in outcomes
        assert all(outcome == "served" or str(outcome).startswith("cut: ") for outcome in outcomes)

    def test_minimal_keeps_metadata_but_no_text(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path, history="minimal") as memory:
            bundle = memory.get_context(prompt, project)
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        assert entry.prompt_text is None
        assert entry.rendered_text is None
        assert entry.trace == {}
        assert entry.prompt_hash
        assert entry.injected is True

    def test_off_writes_nothing(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path, history="off") as memory:
            bundle = memory.get_context(prompt, project)
            assert memory.store.get_bundle(bundle.bundle_id) is None
            assert memory.store.find_bundles(HistoryFilter()) == []

    def test_an_excluded_project_is_never_stored_as_text(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path, excluded_repos=(project,)) as memory:
            bundle = memory.get_context(prompt, project)
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        assert entry.prompt_text is None
        assert entry.rendered_text is None

    def test_a_skipped_prompt_is_recorded_too(self, tmp_path: Path) -> None:
        with service_with(tmp_path) as memory:
            bundle = memory.get_context("Rename the variable res to response", "kairos")
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        assert entry.injected is False
        assert entry.prompt_text == "Rename the variable res to response"

    def test_a_search_is_recorded_with_its_results(self, tmp_path: Path) -> None:
        with service_with(tmp_path) as memory:
            results = memory.search("postgres migrations", limit=3)
            memory.record_search(
                "postgres migrations", results, project_key=None, source="mcp:search_memory"
            )
            [entry] = memory.store.find_bundles(HistoryFilter(source="mcp:search_memory"))

        assert entry.prompt_text == "postgres migrations"
        assert entry.task_type == "search"
        assert len(entry.episodic_ids) == len(results)

    def test_delivery_can_be_confirmed(self, tmp_path: Path) -> None:
        prompt, project = answerable_query()
        with service_with(tmp_path) as memory:
            bundle = memory.get_context(prompt, project)
            memory.mark_delivered(bundle.bundle_id, True)
            entry = memory.store.get_bundle(bundle.bundle_id)

        assert entry is not None
        assert entry.delivered is True


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "pmhome"
    monkeypatch.setenv("PROJECTMIND_HOME", str(path))
    monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
    monkeypatch.setenv("PROJECTMIND_RECENCY_HALF_LIFE_DAYS", str(NO_DECAY))
    reset_settings()
    runner.invoke(app, ["init", "--no-seed"])
    runner.invoke(app, ["profile", "seed", "--activate-all"])
    store = open_store(get_settings())
    load_corpus(store, FixtureCorpus.load(REPO_ROOT / "eval" / "fixtures" / "corpus.yaml"))
    store.close()
    yield path
    reset_settings()


class TestHistoryCommands:
    def ask(self, prompt: str, project: str) -> str:
        result = runner.invoke(app, ["context", prompt, "--project", project, "--raw"])
        assert result.exit_code == 0
        with live_store() as store:
            return str(store.find_bundles(HistoryFilter(limit=1))[0].bundle_id)

    def test_list_shows_what_was_asked(self, home: Path) -> None:
        prompt, project = answerable_query()
        self.ask(prompt, project)
        result = runner.invoke(app, ["history", "list"])
        assert result.exit_code == 0
        assert all(word in result.stdout for word in prompt.split()[:3])
        assert "cli" in result.stdout

    def test_list_filters_to_empty_answers(self, home: Path) -> None:
        prompt, project = answerable_query()
        self.ask(prompt, project)
        self.ask("Rename the variable res to response", "kairos")
        empty = runner.invoke(app, ["history", "list", "--empty"])
        assert "Rename" in empty.stdout
        assert prompt.split()[0] not in empty.stdout

    def test_show_accepts_a_short_prefix_and_prints_the_trace(self, home: Path) -> None:
        prompt, project = answerable_query()
        bundle_id = self.ask(prompt, project)
        result = runner.invoke(app, ["history", "show", bundle_id[:8]])
        assert result.exit_code == 0
        assert "asked" in result.stdout
        assert "candidates" in result.stdout
        assert "served" in result.stdout

    def test_an_unknown_id_is_a_clean_error(self, home: Path) -> None:
        result = runner.invoke(app, ["history", "show", str(uuid4())])
        assert result.exit_code == 1

    def test_replay_of_an_unchanged_system_reports_unchanged(self, home: Path) -> None:
        prompt, project = answerable_query()
        bundle_id = self.ask(prompt, project)
        result = runner.invoke(app, ["history", "replay", bundle_id[:8]])
        assert result.exit_code == 0, result.stdout
        assert "unchanged" in result.stdout

    def test_replay_detects_a_changed_answer(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompt, project = answerable_query()
        bundle_id = self.ask(prompt, project)
        monkeypatch.setattr("projectmind.retrieval.hybrid.MINIMUM_SCORE", 10.0)
        result = runner.invoke(app, ["history", "replay", bundle_id[:8]])
        assert result.exit_code == 2
        assert "changed" in result.stdout

    def test_label_records_feedback_that_reaches_the_report(self, home: Path) -> None:
        prompt, project = answerable_query()
        bundle_id = self.ask(prompt, project)
        result = runner.invoke(
            app, ["history", "label", bundle_id[:8], "--not-useful", "--note", "wrong repo"]
        )
        assert result.exit_code == 0
        with live_store() as store:
            entry = store.get_bundle(UUID(bundle_id))
        assert entry is not None
        assert entry.feedback is False
        assert entry.feedback_note == "wrong repo"

    def test_export_writes_jsonl_with_the_prompt_and_the_answer(
        self, home: Path, tmp_path: Path
    ) -> None:
        prompt, project = answerable_query()
        self.ask(prompt, project)
        destination = tmp_path / "out.jsonl"
        result = runner.invoke(app, ["history", "export", str(destination)])
        assert result.exit_code == 0
        [row] = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
        assert row["prompt"] == prompt
        assert row["served"] is True
        assert row["answer"]

    def test_purge_deletes_only_what_is_old_enough(self, home: Path) -> None:
        prompt, project = answerable_query()
        self.ask(prompt, project)
        kept = runner.invoke(app, ["history", "purge", "--older-than-days", "30", "--yes"])
        assert "deleted 0" in kept.stdout
        gone = runner.invoke(app, ["history", "purge", "--older-than-days", "0", "--yes"])
        assert "deleted 1" in gone.stdout


class TestHookRecordsDelivery:
    def test_the_hook_logs_its_source_session_and_delivery(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompt, project = answerable_query()
        reply = hook_entry.prepare_reply(
            {"prompt": prompt, "cwd": project, "session_id": "sess-42"}
        )
        assert reply.bundle_id is not None
        hook_entry.confirm_delivery(reply.bundle_id, True)

        with live_store() as store:
            entry = store.get_bundle(reply.bundle_id)
        assert entry is not None
        assert entry.source == "hook"
        assert entry.session_id == "sess-42"
        assert entry.delivered is True


class live_store:  # noqa: N801 - a context manager used like a function
    """A short-lived store on the configured home, for assertions."""

    def __enter__(self) -> SqliteStore:
        self.store = open_store(get_settings())
        return self.store  # type: ignore[return-value]

    def __exit__(self, *exc: object) -> None:
        self.store.close()
