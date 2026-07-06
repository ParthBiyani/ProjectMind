"""The evaluation contract.

Two objects matter here. An :class:`EvalQuery` states what a query is and what
a good answer to it looks like, including the answer "nothing". A
:class:`RetrievalResult` is what a system under test returns. The scorer only
ever sees these two, which is what lets the harness outlive every
implementation detail behind it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field, model_validator

from projectmind.models import TaskType as TaskType


class Expectation(StrEnum):
    """What the bundle for a query should contain."""

    NOTHING = "nothing"
    PROFILE_ONLY = "profile_only"
    PROFILE_AND_EPISODIC = "profile_and_episodic"

    @property
    def wants_injection(self) -> bool:
        return self is not Expectation.NOTHING

    @property
    def wants_episodic(self) -> bool:
        return self is Expectation.PROFILE_AND_EPISODIC


class EvalQuery(BaseModel):
    """One hand-labelled query and its expected outcome."""

    model_config = {"frozen": True, "extra": "forbid"}

    id: str
    prompt: str
    project: str = Field(description="Fixture project key the prompt is issued from.")
    task_type: TaskType
    expect: Expectation
    relevant: tuple[str, ...] = Field(
        default=(),
        description="Episodic record ids that belong in the bundle, best first.",
    )
    cross_project: tuple[str, ...] = Field(
        default=(),
        description="Subset of `relevant` that lives in a different project.",
    )
    forbidden: tuple[str, ...] = Field(
        default=(),
        description="Records that look plausible but are wrong to serve here.",
    )
    tags: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _check_consistency(self) -> EvalQuery:
        if not self.expect.wants_injection and self.relevant:
            raise ValueError(f"{self.id}: expects nothing but lists relevant records")
        if self.expect.wants_episodic and not self.relevant:
            raise ValueError(f"{self.id}: expects episodic context but lists none")
        unknown = set(self.cross_project) - set(self.relevant)
        if unknown:
            raise ValueError(f"{self.id}: cross_project ids not in relevant: {sorted(unknown)}")
        overlap = set(self.forbidden) & set(self.relevant)
        if overlap:
            raise ValueError(f"{self.id}: ids both relevant and forbidden: {sorted(overlap)}")
        return self

    @property
    def is_cross_project_case(self) -> bool:
        return bool(self.cross_project)


class EvalSet(BaseModel):
    """A named, versioned collection of queries."""

    model_config = {"extra": "forbid"}

    name: str
    version: str
    description: str = ""
    queries: tuple[EvalQuery, ...]

    @model_validator(mode="after")
    def _check_unique_ids(self) -> EvalSet:
        seen: set[str] = set()
        for query in self.queries:
            if query.id in seen:
                raise ValueError(f"duplicate query id: {query.id}")
            seen.add(query.id)
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> EvalSet:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def by_id(self, query_id: str) -> EvalQuery:
        for query in self.queries:
            if query.id == query_id:
                return query
        raise KeyError(query_id)

    def select(
        self, *, tag: str | None = None, expect: Expectation | None = None
    ) -> list[EvalQuery]:
        return [
            q
            for q in self.queries
            if (tag is None or tag in q.tags) and (expect is None or q.expect is expect)
        ]

    def __len__(self) -> int:
        return len(self.queries)


class RetrievalResult(BaseModel):
    """What a system under test produced for one query.

    Only identifiers are recorded. The scorer never reads record text, so a
    stored result file can be rescored after a metric definition changes.
    """

    model_config = {"extra": "forbid"}

    query_id: str
    profile_ids: tuple[str, ...] = ()
    episodic_ids: tuple[str, ...] = ()
    task_type: TaskType | None = None
    profile_tokens: int = 0
    episodic_tokens: int = 0
    latency_ms: float = 0.0
    skipped_reason: str | None = None

    @property
    def injected(self) -> bool:
        """True when the bundle carried anything at all."""
        return bool(self.profile_ids or self.episodic_ids)

    @property
    def total_tokens(self) -> int:
        return self.profile_tokens + self.episodic_tokens


class SystemUnderTest(Protocol):
    """Anything that can answer an :class:`EvalQuery`.

    Phase 0 ships a null implementation. Later phases pass the real front door.
    """

    name: str

    def __call__(self, query: EvalQuery) -> RetrievalResult: ...


class NullSystem:
    """Serves nothing, ever. The floor every later phase is measured against."""

    name = "null"

    def __call__(self, query: EvalQuery) -> RetrievalResult:
        return RetrievalResult(query_id=query.id, skipped_reason="null baseline")
