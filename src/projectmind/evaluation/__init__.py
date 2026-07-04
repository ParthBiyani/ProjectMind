"""Offline evaluation: the query set, the system contract and the scorer.

This package is deliberately independent of the retrieval stack. It is built
first and scores whatever is plugged into it, starting with a null baseline.
"""

from projectmind.evaluation.spec import (
    EvalQuery,
    EvalSet,
    Expectation,
    NullSystem,
    RetrievalResult,
    SystemUnderTest,
    TaskType,
)

__all__ = [
    "EvalQuery",
    "EvalSet",
    "Expectation",
    "NullSystem",
    "RetrievalResult",
    "SystemUnderTest",
    "TaskType",
]
