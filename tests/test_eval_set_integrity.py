"""Guards on the committed eval set itself.

The labels are the ground truth for every later phase. If they drift, every
number in the repository quietly stops meaning what it says.
"""

from __future__ import annotations

from collections import Counter

from projectmind.evaluation import EvalSet, Expectation, NullSystem
from projectmind.evaluation.fixtures import FixtureCorpus
from projectmind.evaluation.runner import OracleSystem, run


class TestQuerySet:
    def test_has_forty_queries(self, eval_set: EvalSet) -> None:
        assert len(eval_set) == 40

    def test_at_least_a_quarter_expect_no_injection(self, eval_set: EvalSet) -> None:
        no_injection = eval_set.select(expect=Expectation.NOTHING)
        assert len(no_injection) >= 10

    def test_covers_every_task_type(self, eval_set: EvalSet) -> None:
        seen = {query.task_type for query in eval_set.queries}
        assert len(seen) == 6

    def test_enough_cross_project_cases_to_clear_the_target(self, eval_set: EvalSet) -> None:
        cases = [q for q in eval_set.queries if q.is_cross_project_case]
        assert len(cases) / len(eval_set) >= 0.30

    def test_near_misses_exist(self, eval_set: EvalSet) -> None:
        with_forbidden = [q for q in eval_set.queries if q.forbidden]
        assert len(with_forbidden) >= 5

    def test_every_query_carries_a_justification(self, eval_set: EvalSet) -> None:
        missing = [q.id for q in eval_set.queries if not q.note.strip()]
        assert missing == []

    def test_query_ids_are_sequential(self, eval_set: EvalSet) -> None:
        assert [q.id for q in eval_set.queries] == [f"q{i:03d}" for i in range(1, 41)]


class TestLabelsAgainstCorpus:
    def test_every_referenced_project_exists(
        self, eval_set: EvalSet, corpus: FixtureCorpus
    ) -> None:
        keys = {project.key for project in corpus.projects}
        unknown = {q.project for q in eval_set.queries} - keys
        assert unknown == set()

    def test_every_referenced_record_exists(self, eval_set: EvalSet, corpus: FixtureCorpus) -> None:
        ids = {record.id for record in corpus.records}
        referenced = {r for q in eval_set.queries for r in (*q.relevant, *q.forbidden)}
        assert referenced - ids == set()

    def test_cross_project_labels_really_are_cross_project(
        self, eval_set: EvalSet, corpus: FixtureCorpus
    ) -> None:
        wrong = [
            (query.id, record_id)
            for query in eval_set.queries
            for record_id in query.cross_project
            if corpus.project_of(record_id) == query.project
        ]
        assert wrong == []

    def test_debug_queries_prefer_failure_records(
        self, eval_set: EvalSet, corpus: FixtureCorpus
    ) -> None:
        """A debug prompt whose labels are all decisions is probably mislabelled."""
        for query in eval_set.queries:
            if query.task_type != "debug" or not query.relevant:
                continue
            types = Counter(corpus.record(r).type for r in query.relevant)
            assert types["failure"] + types["reversal"] >= 1, query.id


class TestHarnessEndToEnd:
    def test_null_baseline_only_scores_the_no_injection_cases(self, eval_set: EvalSet) -> None:
        report = run(eval_set, NullSystem())
        expected = len(eval_set.select(expect=Expectation.NOTHING)) / len(eval_set)
        assert report.gate_precision == expected
        assert report.precision_at_k == 0.0
        assert report.false_injection_rate == 0.0
        assert report.passed is False

    def test_oracle_ceiling_clears_every_target(self, eval_set: EvalSet) -> None:
        report = run(eval_set, OracleSystem())
        assert report.precision_at_k == 1.0
        assert report.gate_precision == 1.0
        assert report.false_injection_rate == 0.0
        assert report.passed is True

    def test_the_cross_project_target_is_actually_reachable(self, eval_set: EvalSet) -> None:
        """If the ceiling were below the target the set would be unwinnable."""
        report = run(eval_set, OracleSystem())
        assert report.cross_project_hit_rate >= 0.30
