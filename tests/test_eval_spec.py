from __future__ import annotations

import pytest
from pydantic import ValidationError

from projectmind.evaluation import EvalQuery, EvalSet, Expectation, RetrievalResult, TaskType


def _query(**overrides: object) -> EvalQuery:
    base: dict[str, object] = {
        "id": "qx",
        "prompt": "do the thing",
        "project": "kairos",
        "task_type": TaskType.IMPLEMENT,
        "expect": Expectation.PROFILE_AND_EPISODIC,
        "relevant": ("a", "b"),
    }
    base.update(overrides)
    return EvalQuery.model_validate(base)


class TestEvalQuery:
    def test_expectation_flags(self) -> None:
        assert Expectation.NOTHING.wants_injection is False
        assert Expectation.NOTHING.wants_episodic is False
        assert Expectation.PROFILE_ONLY.wants_injection is True
        assert Expectation.PROFILE_ONLY.wants_episodic is False
        assert Expectation.PROFILE_AND_EPISODIC.wants_episodic is True

    def test_nothing_with_relevant_records_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expects nothing"):
            _query(expect=Expectation.NOTHING)

    def test_episodic_expectation_without_labels_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="lists none"):
            _query(relevant=())

    def test_cross_project_must_be_a_subset_of_relevant(self) -> None:
        with pytest.raises(ValidationError, match="not in relevant"):
            _query(cross_project=("c",))

    def test_a_record_cannot_be_relevant_and_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="both relevant and forbidden"):
            _query(forbidden=("a",))

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _query(priority="high")

    def test_is_cross_project_case(self) -> None:
        assert _query().is_cross_project_case is False
        assert _query(cross_project=("a",)).is_cross_project_case is True


class TestEvalSet:
    def test_duplicate_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate query id"):
            EvalSet(
                name="dup",
                version="1",
                queries=(_query(id="q1"), _query(id="q1")),
            )

    def test_select_filters_by_tag_and_expectation(self) -> None:
        eval_set = EvalSet(
            name="s",
            version="1",
            queries=(
                _query(id="q1", tags=("debug",)),
                _query(id="q2", expect=Expectation.NOTHING, relevant=(), tags=("trivial",)),
            ),
        )
        assert [q.id for q in eval_set.select(tag="trivial")] == ["q2"]
        assert [q.id for q in eval_set.select(expect=Expectation.NOTHING)] == ["q2"]
        assert len(eval_set) == 2

    def test_by_id_raises_for_unknown(self) -> None:
        eval_set = EvalSet(name="s", version="1", queries=(_query(id="q1"),))
        assert eval_set.by_id("q1").id == "q1"
        with pytest.raises(KeyError):
            eval_set.by_id("nope")


class TestRetrievalResult:
    def test_injected_is_false_only_when_both_slices_are_empty(self) -> None:
        assert RetrievalResult(query_id="q").injected is False
        assert RetrievalResult(query_id="q", profile_ids=("p1",)).injected is True
        assert RetrievalResult(query_id="q", episodic_ids=("e1",)).injected is True

    def test_total_tokens_sums_both_slices(self) -> None:
        result = RetrievalResult(query_id="q", profile_tokens=100, episodic_tokens=250)
        assert result.total_tokens == 350
