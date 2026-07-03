"""Loader for the evaluation fixture corpus.

The fixture corpus is the "answers you'd want back" half of the eval set. It is
kept separate from the runtime database on purpose: scoring has to be
deterministic and reproducible in CI, which it cannot be if it reads whatever
happens to have been ingested on this machine today.

The models here are intentionally thin. Once the storage layer exists these are
converted into real domain records by a single adapter rather than being used
directly by the retrieval stack.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "eval" / "fixtures"
DEFAULT_CORPUS = FIXTURE_ROOT / "corpus.yaml"

RECORD_TYPES = frozenset({"decision", "failure", "experiment", "research", "reversal"})


class FixtureProject(BaseModel):
    """A project the fixture records belong to."""

    model_config = {"frozen": True, "extra": "forbid"}

    key: str
    name: str
    git_remote: str
    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


class FixtureRecord(BaseModel):
    """One episodic record with the provenance every record must carry."""

    model_config = {"frozen": True, "extra": "forbid"}

    id: str
    project: str
    type: str
    what: str
    why: str = ""
    rejected_alternatives: tuple[str, ...] = ()
    outcome: str = ""
    entities: tuple[str, ...] = ()
    source_type: str
    source_url: str
    occurred_at: date
    confidence: float = Field(ge=0.0, le=1.0)
    is_inference: bool = False

    @model_validator(mode="after")
    def _check_type(self) -> FixtureRecord:
        if self.type not in RECORD_TYPES:
            raise ValueError(f"{self.id}: unknown record type {self.type!r}")
        return self

    @property
    def searchable_text(self) -> str:
        """Everything a lexical index should see for this record."""
        parts = [self.what, self.why, self.outcome, " ".join(self.rejected_alternatives)]
        return " ".join(p for p in parts if p)


class FixtureCorpus(BaseModel):
    """Projects plus records, with referential integrity enforced on load."""

    model_config = {"extra": "forbid"}

    name: str
    version: str
    description: str = ""
    projects: tuple[FixtureProject, ...]
    records: tuple[FixtureRecord, ...]

    @model_validator(mode="after")
    def _check_references(self) -> FixtureCorpus:
        keys = {p.key for p in self.projects}
        seen: set[str] = set()
        for record in self.records:
            if record.project not in keys:
                raise ValueError(f"{record.id}: unknown project {record.project!r}")
            if record.id in seen:
                raise ValueError(f"duplicate record id: {record.id}")
            seen.add(record.id)
        return self

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CORPUS) -> FixtureCorpus:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def record(self, record_id: str) -> FixtureRecord:
        for candidate in self.records:
            if candidate.id == record_id:
                return candidate
        raise KeyError(record_id)

    def project(self, key: str) -> FixtureProject:
        for candidate in self.projects:
            if candidate.key == key:
                return candidate
        raise KeyError(key)

    def project_of(self, record_id: str) -> str:
        return self.record(record_id).project

    def records_for(self, project_key: str) -> list[FixtureRecord]:
        return [r for r in self.records if r.project == project_key]

    @property
    def entity_index(self) -> dict[str, list[str]]:
        """Entity to record ids. The prompt gate is built on exactly this shape."""
        index: dict[str, list[str]] = {}
        for record in self.records:
            for entity in record.entities:
                index.setdefault(entity, []).append(record.id)
        return index
