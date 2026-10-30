"""Scoring the front door against questions about your own material.

The synthetic evaluation answers "does the retrieval logic work on a corpus I
wrote to test it?". This one answers "does it work on what I actually have?".

A query names what a correct answer must cite, as substrings of the citation
(`CLAUDE.md`, a project folder, a commit hash), not as record ids, so the labels
keep meaning across re-ingestion. A query can instead expect *nothing*: the
topic is not in the corpus, and the right answer is silence.

The query file belongs to its author and lives outside version control; only the
schema, this scorer and aggregate results are committed.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from projectmind.service import MemoryService


class RealQuery(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    prompt: str
    #: Where the question is asked from (a directory), as a hook would report it.
    project: str = ""
    expect: Literal["something", "nothing"] = "something"
    #: A correct answer cites at least one source whose citation contains one of these.
    any_of: tuple[str, ...] = ()
    #: No served source may contain any of these (the near-misses).
    forbidden: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> RealQuery:
        if self.expect == "something" and not self.any_of:
            raise ValueError(f"{self.id}: a query that expects an answer needs any_of")
        if self.expect == "nothing" and self.any_of:
            raise ValueError(f"{self.id}: a query that expects nothing cannot have any_of")
        return self


class RealSet(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    version: str = "1"
    description: str = ""
    queries: tuple[RealQuery, ...] = Field(min_length=1)

    @classmethod
    def from_yaml(cls, path: str | Path) -> RealSet:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    @model_validator(mode="after")
    def _unique_ids(self) -> RealSet:
        ids = [query.id for query in self.queries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate query ids")
        return self


class QueryOutcome(BaseModel):
    id: str
    prompt: str
    expect: str
    passed: bool
    served: tuple[str, ...]
    reason: str
    tokens: int
    latency_ms: float


class RealReport(BaseModel):
    name: str
    queries: int
    answerable: int
    silent: int
    hit_rate: float
    served_precision: float
    silence_rate: float
    false_injection_rate: float
    forbidden_served: int
    mean_tokens: float
    p95_latency_ms: float
    outcomes: tuple[QueryOutcome, ...]

    @property
    def failures(self) -> tuple[QueryOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.passed)


def citations(bundle_evidence: object, bundle_episodic: object) -> tuple[str, ...]:
    """What a bundle cites: evidence citations and episodic source links."""
    found: list[str] = []
    for item in bundle_evidence:  # type: ignore[attr-defined]
        found.append(item.citation)
    for record in bundle_episodic:  # type: ignore[attr-defined]
        found.append(record.source_url)
    return tuple(found)


def run_real(memory: MemoryService, queries: RealSet) -> RealReport:
    outcomes: list[QueryOutcome] = []
    latencies: list[float] = []
    tokens: list[int] = []

    for query in queries.queries:
        started = time.perf_counter()
        bundle = memory.get_context(query.prompt, query.project or ".", log_bundle=False)
        latencies.append((time.perf_counter() - started) * 1000.0)
        served = citations(bundle.evidence, bundle.episodic)
        tokens.append(bundle.total_tokens)

        bad = [c for c in served if any(f in c for f in query.forbidden)]
        if query.expect == "nothing":
            passed = not served
            reason = (
                "silent, as expected"
                if passed
                else f"served {len(served)} for a topic not in the corpus"
            )
        else:
            matched = any(any(needle in c for needle in query.any_of) for c in served)
            passed = matched
            reason = (
                "cited an expected source"
                if matched
                else ("served nothing" if not served else "served sources, none expected")
            )
        if bad:
            passed = False
            reason = f"served a forbidden source ({bad[0]})"
        outcomes.append(
            QueryOutcome(
                id=query.id,
                prompt=query.prompt,
                expect=query.expect,
                passed=passed,
                served=served,
                reason=reason,
                tokens=bundle.total_tokens,
                latency_ms=round(latencies[-1], 1),
            )
        )

    answerable = [q for q in queries.queries if q.expect == "something"]
    silent = [q for q in queries.queries if q.expect == "nothing"]
    by_id = {outcome.id: outcome for outcome in outcomes}

    hits = sum(1 for q in answerable if by_id[q.id].passed)
    silent_ok = sum(1 for q in silent if by_id[q.id].passed)

    relevant_served = total_served = 0
    for query in answerable:
        for citation in by_id[query.id].served:
            total_served += 1
            relevant_served += int(any(needle in citation for needle in query.any_of))

    forbidden_count = sum(
        1
        for query in queries.queries
        for citation in by_id[query.id].served
        if any(f in citation for f in query.forbidden)
    )
    return RealReport(
        name=queries.name,
        queries=len(queries.queries),
        answerable=len(answerable),
        silent=len(silent),
        hit_rate=round(hits / len(answerable), 3) if answerable else 0.0,
        served_precision=round(relevant_served / total_served, 3) if total_served else 0.0,
        silence_rate=round(silent_ok / len(silent), 3) if silent else 1.0,
        false_injection_rate=round(1 - silent_ok / len(silent), 3) if silent else 0.0,
        forbidden_served=forbidden_count,
        mean_tokens=round(statistics.fmean(tokens), 1),
        p95_latency_ms=round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1),
        outcomes=tuple(outcomes),
    )
