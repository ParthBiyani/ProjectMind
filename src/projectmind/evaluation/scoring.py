"""Metrics.

Every metric here is computed from identifiers alone, so a stored result file
can be rescored after a definition changes. Where this module and the PRD could
drift, the PRD wins and the difference is spelled out in a comment.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from projectmind.evaluation.spec import EvalQuery, EvalSet, RetrievalResult

DEFAULT_K = 5

#: Budget caps from PRD section 6.3. A bundle over any of these is a defect
#: regardless of how relevant its contents are.
PROFILE_TOKEN_CAP = 800
EPISODIC_TOKEN_CAP = 1500
TOTAL_TOKEN_CAP = 2300

#: Targets from PRD section 8. ``False`` in the second position means lower is
#: better, so the comparison flips.
TARGETS: dict[str, tuple[float, bool]] = {
    "precision_at_k": (0.70, True),
    "cross_project_hit_rate": (0.30, True),
    "gate_precision": (0.80, True),
    "false_injection_rate": (0.10, False),
}


class QueryScore(BaseModel):
    """Per-query detail. Kept in the report so a regression can be traced."""

    model_config = {"extra": "forbid"}

    query_id: str
    expected_injection: bool
    expected_episodic: bool
    injected: bool
    served_episodic: int
    hits: int
    misses: int
    forbidden_served: tuple[str, ...] = ()
    gate_correct: bool
    episodic_gate_correct: bool
    precision_at_k: float | None = None
    ndcg_at_k: float | None = None
    reciprocal_rank: float | None = None
    cross_project_case: bool = False
    cross_project_hit: bool = False
    profile_tokens: int = 0
    episodic_tokens: int = 0
    over_budget: bool = False
    latency_ms: float = 0.0


class ScoreReport(BaseModel):
    """A full run. Serialised to `eval/baselines/` and committed."""

    model_config = {"extra": "forbid"}

    system: str
    eval_set: str
    eval_version: str
    generated_at: datetime
    k: int
    n_queries: int

    # headline metrics
    precision_at_k: float
    cross_project_hit_rate: float
    gate_precision: float
    false_injection_rate: float

    # diagnostics
    episodic_gate_precision: float
    cross_project_recall: float
    ndcg_at_k: float
    mrr: float
    forbidden_rate: float
    unwanted_bundle_rate: float
    empty_when_expected_rate: float
    mean_total_tokens: float
    max_total_tokens: int
    budget_violations: int
    p50_latency_ms: float
    p95_latency_ms: float

    per_query: tuple[QueryScore, ...] = Field(default=(), repr=False)

    @property
    def targets_met(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for name, (threshold, higher_is_better) in TARGETS.items():
            value = float(getattr(self, name))
            out[name] = value >= threshold if higher_is_better else value <= threshold
        return out

    @property
    def passed(self) -> bool:
        return all(self.targets_met.values())

    def summary_rows(self) -> list[tuple[str, str, str, str]]:
        """(metric, value, target, verdict) rows for terminal rendering."""
        rows: list[tuple[str, str, str, str]] = []
        met = self.targets_met
        for name, (threshold, higher_is_better) in TARGETS.items():
            value = float(getattr(self, name))
            comparator = ">=" if higher_is_better else "<="
            rows.append(
                (
                    name,
                    f"{value:.3f}",
                    f"{comparator} {threshold:.2f}",
                    "pass" if met[name] else "FAIL",
                )
            )
        return rows


def _dcg(gains: Sequence[float]) -> float:
    return sum(gain / math.log2(position + 2) for position, gain in enumerate(gains))


def ndcg_at_k(served: Sequence[str], relevant: Sequence[str], k: int = DEFAULT_K) -> float:
    """Binary-gain nDCG. Ideal ordering is the label order, which is best-first."""
    if not relevant:
        return 0.0
    relevant_set = set(relevant)
    gains = [1.0 if record_id in relevant_set else 0.0 for record_id in served[:k]]
    ideal = [1.0] * min(len(relevant), k)
    denominator = _dcg(ideal)
    return _dcg(gains) / denominator if denominator else 0.0


def reciprocal_rank(served: Sequence[str], relevant: Sequence[str]) -> float:
    relevant_set = set(relevant)
    for position, record_id in enumerate(served, start=1):
        if record_id in relevant_set:
            return 1.0 / position
    return 0.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def score_query(
    query: EvalQuery,
    result: RetrievalResult,
    *,
    project_of: Callable[[str], str | None],
    k: int = DEFAULT_K,
) -> QueryScore:
    """Score one query against one result."""
    served = list(result.episodic_ids[:k])
    relevant = set(query.relevant)
    forbidden = set(query.forbidden)

    hits = sum(1 for record_id in served if record_id in relevant)
    served_forbidden = tuple(record_id for record_id in served if record_id in forbidden)

    cross_project_hit = any(
        (owner := project_of(record_id)) is not None
        and owner != query.project
        and record_id in relevant
        for record_id in served
    )

    precision = hits / len(served) if served else 0.0
    over_budget = (
        result.profile_tokens > PROFILE_TOKEN_CAP
        or result.episodic_tokens > EPISODIC_TOKEN_CAP
        or result.total_tokens > TOTAL_TOKEN_CAP
    )

    return QueryScore(
        query_id=query.id,
        expected_injection=query.expect.wants_injection,
        expected_episodic=query.expect.wants_episodic,
        injected=result.injected,
        served_episodic=len(served),
        hits=hits,
        misses=len(served) - hits,
        forbidden_served=served_forbidden,
        gate_correct=result.injected == query.expect.wants_injection,
        episodic_gate_correct=bool(served) == query.expect.wants_episodic,
        precision_at_k=precision if query.expect.wants_episodic else None,
        ndcg_at_k=ndcg_at_k(served, query.relevant, k) if query.expect.wants_episodic else None,
        reciprocal_rank=(
            reciprocal_rank(served, query.relevant) if query.expect.wants_episodic else None
        ),
        cross_project_case=query.is_cross_project_case,
        cross_project_hit=cross_project_hit,
        profile_tokens=result.profile_tokens,
        episodic_tokens=result.episodic_tokens,
        over_budget=over_budget,
        latency_ms=result.latency_ms,
    )


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return statistics.fmean(collected) if collected else 0.0


def score_run(
    eval_set: EvalSet,
    results: Sequence[RetrievalResult],
    *,
    system: str,
    project_of: Callable[[str], str | None],
    k: int = DEFAULT_K,
) -> ScoreReport:
    """Aggregate per-query scores into a report.

    ``results`` must cover the whole set; a missing result is a failure to
    answer, not an excuse to shrink the denominator.
    """
    by_id = {result.query_id: result for result in results}
    missing = [query.id for query in eval_set.queries if query.id not in by_id]
    if missing:
        raise ValueError(f"no result for queries: {', '.join(missing)}")

    scores = [
        score_query(query, by_id[query.id], project_of=project_of, k=k)
        for query in eval_set.queries
    ]

    episodic_cases = [s for s in scores if s.expected_episodic]
    total_served = sum(s.served_episodic for s in scores)
    total_wrong = sum(s.served_episodic - s.hits for s in scores)

    # A bundle is "unwanted" when anything at all was served for a query whose
    # correct answer was nothing. This is the query-level companion to the
    # record-level false injection rate.
    injected_bundles = [s for s in scores if s.injected]
    unwanted_bundles = [s for s in injected_bundles if not s.expected_injection]

    forbidden_opportunities = sum(1 for query in eval_set.queries for _ in query.forbidden)
    forbidden_hits = sum(len(s.forbidden_served) for s in scores)

    latencies = [s.latency_ms for s in scores]
    totals = [s.profile_tokens + s.episodic_tokens for s in scores]

    return ScoreReport(
        system=system,
        eval_set=eval_set.name,
        eval_version=eval_set.version,
        generated_at=datetime.now(UTC),
        k=k,
        n_queries=len(scores),
        precision_at_k=_mean(s.precision_at_k or 0.0 for s in episodic_cases),
        # Denominator is every query, matching the PRD's "30% of queries".
        # `cross_project_recall` below uses the cross-project cases instead and
        # is the number to look at when diagnosing the ranker.
        cross_project_hit_rate=(
            sum(1 for s in scores if s.cross_project_hit) / len(scores) if scores else 0.0
        ),
        gate_precision=_mean(1.0 if s.gate_correct else 0.0 for s in scores),
        false_injection_rate=(total_wrong / total_served if total_served else 0.0),
        episodic_gate_precision=_mean(1.0 if s.episodic_gate_correct else 0.0 for s in scores),
        cross_project_recall=_mean(
            1.0 if s.cross_project_hit else 0.0 for s in scores if s.cross_project_case
        ),
        ndcg_at_k=_mean(s.ndcg_at_k or 0.0 for s in episodic_cases),
        mrr=_mean(s.reciprocal_rank or 0.0 for s in episodic_cases),
        forbidden_rate=(
            forbidden_hits / forbidden_opportunities if forbidden_opportunities else 0.0
        ),
        unwanted_bundle_rate=(
            len(unwanted_bundles) / len(injected_bundles) if injected_bundles else 0.0
        ),
        empty_when_expected_rate=_mean(
            1.0 if not s.injected else 0.0 for s in scores if s.expected_injection
        ),
        mean_total_tokens=_mean(float(t) for t in totals),
        max_total_tokens=max(totals) if totals else 0,
        budget_violations=sum(1 for s in scores if s.over_budget),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        per_query=tuple(scores),
    )


def compare(before: ScoreReport, after: ScoreReport) -> list[tuple[str, float, float, float]]:
    """(metric, before, after, delta) for every headline and diagnostic metric."""
    names = [
        "precision_at_k",
        "cross_project_hit_rate",
        "gate_precision",
        "false_injection_rate",
        "episodic_gate_precision",
        "cross_project_recall",
        "ndcg_at_k",
        "mrr",
        "forbidden_rate",
        "unwanted_bundle_rate",
        "mean_total_tokens",
        "p95_latency_ms",
    ]
    rows: list[tuple[str, float, float, float]] = []
    for name in names:
        old = float(getattr(before, name))
        new = float(getattr(after, name))
        rows.append((name, old, new, new - old))
    return rows
