from __future__ import annotations

import pytest

from projectmind.evaluation import EvalQuery, EvalSet, Expectation, RetrievalResult, TaskType
from projectmind.evaluation.scoring import (
    ndcg_at_k,
    reciprocal_rank,
    score_query,
    score_run,
)

OWNERS = {
    "a1": "alpha",
    "a2": "alpha",
    "b1": "beta",
    "b2": "beta",
    "c1": "gamma",
}


def owner_of(record_id: str) -> str | None:
    return OWNERS.get(record_id)


def _query(**overrides: object) -> EvalQuery:
    base: dict[str, object] = {
        "id": "q1",
        "prompt": "p",
        "project": "alpha",
        "task_type": TaskType.DEBUG,
        "expect": Expectation.PROFILE_AND_EPISODIC,
        "relevant": ("a1", "b1"),
    }
    base.update(overrides)
    return EvalQuery.model_validate(base)


class TestRankingPrimitives:
    def test_ndcg_is_one_for_a_perfect_prefix(self) -> None:
        assert ndcg_at_k(["a1", "b1"], ["a1", "b1"]) == pytest.approx(1.0)

    def test_ndcg_penalises_a_relevant_result_placed_late(self) -> None:
        early = ndcg_at_k(["a1", "x", "y"], ["a1"])
        late = ndcg_at_k(["x", "y", "a1"], ["a1"])
        assert early > late > 0.0

    def test_ndcg_is_zero_without_labels(self) -> None:
        assert ndcg_at_k(["a1"], []) == 0.0

    def test_ndcg_respects_k(self) -> None:
        assert ndcg_at_k(["x", "x", "x", "x", "x", "a1"], ["a1"], k=5) == 0.0

    def test_reciprocal_rank_uses_the_first_hit(self) -> None:
        assert reciprocal_rank(["x", "a1", "b1"], ["a1", "b1"]) == pytest.approx(0.5)
        assert reciprocal_rank(["x", "y"], ["a1"]) == 0.0


class TestScoreQuery:
    def test_counts_hits_misses_and_forbidden(self) -> None:
        query = _query(forbidden=("c1",))
        result = RetrievalResult(query_id="q1", episodic_ids=("a1", "c1", "z9"))
        score = score_query(query, result, project_of=owner_of)
        assert (score.hits, score.misses) == (1, 2)
        assert score.forbidden_served == ("c1",)
        assert score.precision_at_k == pytest.approx(1 / 3)

    def test_cross_project_hit_requires_a_relevant_record_from_elsewhere(self) -> None:
        query = _query(cross_project=("b1",))
        same_project_only = score_query(
            query, RetrievalResult(query_id="q1", episodic_ids=("a1",)), project_of=owner_of
        )
        assert same_project_only.cross_project_hit is False

        with_cross = score_query(
            query, RetrievalResult(query_id="q1", episodic_ids=("b1",)), project_of=owner_of
        )
        assert with_cross.cross_project_hit is True

    def test_an_irrelevant_record_from_another_project_is_not_a_hit(self) -> None:
        query = _query(relevant=("a1",), cross_project=())
        score = score_query(
            query, RetrievalResult(query_id="q1", episodic_ids=("b2",)), project_of=owner_of
        )
        assert score.cross_project_hit is False

    def test_empty_bundle_for_an_episodic_query_scores_zero_not_none(self) -> None:
        score = score_query(_query(), RetrievalResult(query_id="q1"), project_of=owner_of)
        assert score.precision_at_k == 0.0
        assert score.injected is False
        assert score.gate_correct is False

    def test_ranking_metrics_are_none_for_non_episodic_queries(self) -> None:
        query = _query(expect=Expectation.PROFILE_ONLY, relevant=())
        score = score_query(
            query, RetrievalResult(query_id="q1", profile_ids=("p",)), project_of=owner_of
        )
        assert score.precision_at_k is None
        assert score.ndcg_at_k is None
        assert score.gate_correct is True

    def test_budget_violation_is_flagged_per_slice(self) -> None:
        score = score_query(
            _query(),
            RetrievalResult(query_id="q1", episodic_ids=("a1",), profile_tokens=801),
            project_of=owner_of,
        )
        assert score.over_budget is True

    def test_only_the_top_k_records_are_scored(self) -> None:
        query = _query(relevant=("c1",))
        result = RetrievalResult(query_id="q1", episodic_ids=("x", "x", "x", "x", "x", "c1"))
        score = score_query(query, result, project_of=owner_of, k=5)
        assert score.served_episodic == 5
        assert score.hits == 0


class TestScoreRun:
    def _set(self) -> EvalSet:
        return EvalSet(
            name="t",
            version="1",
            queries=(
                _query(id="q1", relevant=("a1", "b1"), cross_project=("b1",)),
                _query(id="q2", expect=Expectation.NOTHING, relevant=()),
                _query(id="q3", expect=Expectation.PROFILE_ONLY, relevant=()),
            ),
        )

    def test_missing_results_raise_rather_than_shrink_the_denominator(self) -> None:
        with pytest.raises(ValueError, match="no result for queries: q2, q3"):
            score_run(
                self._set(),
                [RetrievalResult(query_id="q1")],
                system="partial",
                project_of=owner_of,
            )

    def test_a_perfect_run_meets_every_target(self) -> None:
        report = score_run(
            self._set(),
            [
                RetrievalResult(query_id="q1", profile_ids=("p",), episodic_ids=("a1", "b1")),
                RetrievalResult(query_id="q2"),
                RetrievalResult(query_id="q3", profile_ids=("p",)),
            ],
            system="perfect",
            project_of=owner_of,
        )
        assert report.precision_at_k == pytest.approx(1.0)
        assert report.gate_precision == pytest.approx(1.0)
        assert report.false_injection_rate == 0.0
        assert report.cross_project_hit_rate == pytest.approx(1 / 3)
        assert report.passed is True

    def test_serving_nothing_everywhere_keeps_false_injection_at_zero(self) -> None:
        report = score_run(
            self._set(),
            [RetrievalResult(query_id=q.id) for q in self._set().queries],
            system="null",
            project_of=owner_of,
        )
        assert report.false_injection_rate == 0.0
        assert report.empty_when_expected_rate == pytest.approx(1.0)
        assert report.gate_precision == pytest.approx(1 / 3)
        assert report.passed is False

    def test_false_injection_is_counted_per_record_not_per_query(self) -> None:
        report = score_run(
            self._set(),
            [
                RetrievalResult(query_id="q1", episodic_ids=("a1", "z1", "z2", "z3")),
                RetrievalResult(query_id="q2"),
                RetrievalResult(query_id="q3", profile_ids=("p",)),
            ],
            system="noisy",
            project_of=owner_of,
        )
        assert report.false_injection_rate == pytest.approx(0.75)

    def test_injecting_into_a_no_injection_query_shows_up_as_an_unwanted_bundle(self) -> None:
        report = score_run(
            self._set(),
            [
                RetrievalResult(query_id="q1", episodic_ids=("a1", "b1")),
                RetrievalResult(query_id="q2", profile_ids=("p",)),
                RetrievalResult(query_id="q3", profile_ids=("p",)),
            ],
            system="eager",
            project_of=owner_of,
        )
        assert report.unwanted_bundle_rate == pytest.approx(1 / 3)
        assert report.gate_precision == pytest.approx(2 / 3)

    def test_latency_percentiles_are_reported(self) -> None:
        report = score_run(
            self._set(),
            [
                RetrievalResult(query_id="q1", latency_ms=10.0),
                RetrievalResult(query_id="q2", latency_ms=20.0),
                RetrievalResult(query_id="q3", latency_ms=90.0),
            ],
            system="timed",
            project_of=owner_of,
        )
        assert report.p50_latency_ms == pytest.approx(20.0)
        assert report.p95_latency_ms == pytest.approx(90.0)
