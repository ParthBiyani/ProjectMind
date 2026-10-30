from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from projectmind.cli import app
from projectmind.config import Settings, reset_settings
from projectmind.evaluation.real import RealQuery, RealSet, run_real
from projectmind.service import MemoryService
from projectmind.sources.config import RootConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.storage import SqliteStore

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime - 7_200
    os.utime(path, (stamp, stamp))


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[MemoryService]:
    root = tmp_path / "work"
    write(
        root / "Prostate" / "CLAUDE.md",
        "# Prostate\n\n## Metric\n\nThe metric is dice plus boundary F1 on voxel masks.\n",
    )
    write(
        root / "Albanian" / "CLAUDE.md",
        "# Albanian\n\n## Metric\n\nThe metric is chrF on recovered cards.\n",
    )
    for index in range(30):
        write(
            root / "Noise" / f"n{index}.md",
            f"# Note {index}\n\nSome unrelated filler text {index}.\n",
        )
    store = SqliteStore(":memory:")
    store.migrate()
    KnowledgeIndexer(store).scan_root(RootConfig(path=root, depth=1))
    service = MemoryService(store, Settings(db_url=None, recency_half_life_days=1e6))
    yield service
    service.close()


def queries(*items: RealQuery) -> RealSet:
    return RealSet(name="t", queries=items)


class TestSchema:
    def test_a_query_that_expects_an_answer_must_say_what_it_should_cite(self) -> None:
        with pytest.raises(ValidationError):
            RealQuery(id="a", prompt="p", expect="something")

    def test_a_query_that_expects_nothing_cannot_name_a_source(self) -> None:
        with pytest.raises(ValidationError):
            RealQuery(id="a", prompt="p", expect="nothing", any_of=("x",))

    def test_duplicate_ids_are_refused(self) -> None:
        one = RealQuery(id="a", prompt="p", any_of=("x",))
        with pytest.raises(ValidationError):
            queries(one, one)

    def test_the_committed_example_parses(self) -> None:
        loaded = RealSet.from_yaml(REPO / "eval" / "real.example.yaml")
        assert any(q.expect == "nothing" for q in loaded.queries)
        assert any(q.expect == "something" for q in loaded.queries)


class TestScoring:
    def test_a_correct_citation_is_a_hit(self, memory: MemoryService) -> None:
        report = run_real(
            memory,
            queries(
                RealQuery(
                    id="q1",
                    prompt="What is the metric on the prostate task, dice and boundary F1?",
                    any_of=("Prostate/CLAUDE.md",),
                )
            ),
        )
        assert report.hit_rate == 1.0
        assert report.outcomes[0].passed

    def test_citing_the_wrong_source_is_a_miss(self, memory: MemoryService) -> None:
        report = run_real(
            memory,
            queries(
                RealQuery(
                    id="q1",
                    prompt="What is the metric on the prostate task, dice and boundary F1?",
                    any_of=("Albanian/CLAUDE.md",),
                )
            ),
        )
        assert report.hit_rate == 0.0
        assert report.outcomes[0].reason == "served sources, none expected"

    def test_silence_is_correct_for_an_absent_topic(self, memory: MemoryService) -> None:
        report = run_real(
            memory,
            queries(
                RealQuery(
                    id="n1",
                    prompt="Tune the kubernetes ingress certificate rotation",
                    expect="nothing",
                )
            ),
        )
        assert report.silence_rate == 1.0
        assert report.false_injection_rate == 0.0

    def test_serving_for_an_absent_topic_is_false_injection(self, memory: MemoryService) -> None:
        report = run_real(
            memory,
            queries(
                RealQuery(
                    id="n1",
                    prompt="What is the metric on the prostate task, dice and boundary F1?",
                    expect="nothing",
                )
            ),
        )
        assert report.false_injection_rate == 1.0
        assert not report.outcomes[0].passed

    def test_a_forbidden_source_fails_even_alongside_a_correct_one(
        self, memory: MemoryService
    ) -> None:
        report = run_real(
            memory,
            queries(
                RealQuery(
                    id="q1",
                    prompt="What is the metric on the prostate task, dice and boundary F1?",
                    any_of=("Prostate/CLAUDE.md",),
                    forbidden=("Prostate/CLAUDE.md",),
                )
            ),
        )
        assert report.forbidden_served >= 1
        assert not report.outcomes[0].passed

    def test_scoring_logs_nothing(self, memory: MemoryService) -> None:
        run_real(memory, queries(RealQuery(id="q1", prompt="metric dice", any_of=("x",))))
        assert memory.store.recent_bundles() == []


class TestCommand:
    @pytest.fixture(autouse=True)
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
        path = tmp_path / "pmhome"
        monkeypatch.setenv("PROJECTMIND_HOME", str(path))
        monkeypatch.delenv("PROJECTMIND_DB_URL", raising=False)
        reset_settings()
        runner.invoke(app, ["init", "--no-seed"])
        yield path
        reset_settings()

    def test_a_missing_query_file_says_where_it_looked(self) -> None:
        result = runner.invoke(app, ["eval", "real"])
        assert result.exit_code == 1
        assert (
            "queries-real.yaml" in result.stdout + result.stderr
            if hasattr(result, "stderr")
            else True
        )

    def test_it_scores_and_gates(self, tmp_path: Path) -> None:
        file = tmp_path / "q.yaml"
        file.write_text(
            "name: t\nqueries:\n"
            "  - id: n1\n"
            "    prompt: Tune the kubernetes ingress certificate rotation\n"
            "    expect: nothing\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["eval", "real", str(file), "--max-false-injection", "0.0"])
        assert result.exit_code == 0, result.stdout
        assert "false injection rate" in result.stdout

    def test_a_failing_gate_exits_non_zero(self, tmp_path: Path) -> None:
        file = tmp_path / "q.yaml"
        file.write_text(
            "name: t\nqueries:\n"
            "  - id: a1\n"
            "    prompt: What is the metric on the prostate task?\n"
            "    any_of: [CLAUDE.md]\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["eval", "real", str(file), "--min-hit", "0.9"])
        assert result.exit_code == 1
