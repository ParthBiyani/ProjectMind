"""Run a system under test across the whole eval set and persist the report."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from projectmind.evaluation.fixtures import FixtureCorpus
from projectmind.evaluation.scoring import ScoreReport, score_run
from projectmind.evaluation.spec import EvalQuery, EvalSet, RetrievalResult, SystemUnderTest

BASELINE_DIR = Path(__file__).resolve().parents[3] / "eval" / "baselines"


def run(
    eval_set: EvalSet,
    system: SystemUnderTest,
    *,
    corpus: FixtureCorpus | None = None,
    k: int = 5,
) -> ScoreReport:
    """Execute every query and score the run.

    Latency is measured here rather than trusted from the system, so a system
    that forgets to populate `latency_ms` is still timed.
    """
    corpus = corpus or FixtureCorpus.load()
    owner_of = _project_lookup(corpus)

    results: list[RetrievalResult] = []
    for query in eval_set.queries:
        started = time.perf_counter()
        result = system(query)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if result.query_id != query.id:
            raise ValueError(f"system returned result for {result.query_id}, expected {query.id}")
        if not result.latency_ms:
            result = result.model_copy(update={"latency_ms": elapsed_ms})
        results.append(result)

    return score_run(eval_set, results, system=system.name, project_of=owner_of, k=k)


def _project_lookup(corpus: FixtureCorpus) -> Callable[[str], str | None]:
    index = {record.id: record.project for record in corpus.records}
    return index.get


def save(report: ScoreReport, path: str | Path) -> Path:
    """Write a report as indented JSON so diffs between phases stay readable."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return destination


def load(path: str | Path) -> ScoreReport:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return ScoreReport.model_validate(raw)


class CallableSystem:
    """Adapt a plain function into a named :class:`SystemUnderTest`."""

    def __init__(self, name: str, fn: Callable[[EvalQuery], RetrievalResult]) -> None:
        self.name = name
        self._fn = fn

    def __call__(self, query: EvalQuery) -> RetrievalResult:
        return self._fn(query)


class OracleSystem:
    """Serves exactly the labelled answer. The ceiling, not a real system.

    Useful for two things: proving the scorer reports 1.0 when it should, and
    showing what the labels imply for the cross-project target, which is capped
    by how many cross-project cases the set contains.
    """

    name = "oracle"

    def __init__(self, k: int = 5) -> None:
        self._k = k

    def __call__(self, query: EvalQuery) -> RetrievalResult:
        if not query.expect.wants_injection:
            return RetrievalResult(query_id=query.id, skipped_reason="trivial prompt")
        episodic = query.relevant[: self._k] if query.expect.wants_episodic else ()
        return RetrievalResult(
            query_id=query.id,
            profile_ids=("oracle-profile",),
            episodic_ids=episodic,
            task_type=query.task_type,
            profile_tokens=120,
            episodic_tokens=90 * len(episodic),
        )
