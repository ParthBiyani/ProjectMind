"""Loading the starter profile.

Seeded statements arrive as `proposed`, never `active`. That is not caution for
its own sake: the architecture rule is that profile statements only ever become
active through human approval, and a seed file that activated itself would be
the one path around it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from projectmind.logging import get_logger
from projectmind.models import (
    DEFAULT_TTL_DAYS,
    Category,
    Origin,
    ProfileStatement,
    StatementStatus,
    utcnow,
)
from projectmind.profile.lifecycle import SEED_EVIDENCE
from projectmind.profile.repository import ProfileRepository

log = get_logger(__name__)

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "profile_seed.yaml"


class SeedStatement(BaseModel):
    """One entry in the seed file."""

    model_config = {"extra": "forbid"}

    statement: str
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    entities: tuple[str, ...] = ()
    scope: dict[str, Any] | None = None
    ttl_days: int | None = None
    note: str = ""

    @model_validator(mode="after")
    def _reject_vague_statements(self) -> SeedStatement:
        """A statement that cannot be contradicted cannot be maintained."""
        if len(self.statement.split()) < 5:
            raise ValueError(f"statement is too vague to be falsifiable: {self.statement!r}")
        return self

    def to_statement(self, *, now: datetime | None = None) -> ProfileStatement:
        moment = now or utcnow()
        return ProfileStatement(
            statement=self.statement,
            category=self.category,
            scope=self.scope,
            evidence_refs=(SEED_EVIDENCE,),
            confidence=self.confidence,
            status=StatementStatus.PROPOSED,
            entities=self.entities,
            created_at=moment,
            last_confirmed_at=moment,
            ttl_days=self.ttl_days or DEFAULT_TTL_DAYS[self.category],
            origin=Origin.MANUAL,
        )


class SeedFile(BaseModel):
    model_config = {"extra": "forbid"}

    version: str
    description: str = ""
    statements: tuple[SeedStatement, ...]

    @classmethod
    def load(cls, path: str | Path = SEED_PATH) -> SeedFile:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def by_category(self, category: Category) -> list[SeedStatement]:
        return [s for s in self.statements if s.category is category]

    def __len__(self) -> int:
        return len(self.statements)

    def __iter__(self) -> Iterator[SeedStatement]:  # type: ignore[override]
        return iter(self.statements)


@dataclass(slots=True)
class SeedResult:
    loaded: int = 0
    skipped_duplicates: int = 0
    activated: int = 0

    @property
    def summary(self) -> str:
        parts = [f"{self.loaded} loaded"]
        if self.skipped_duplicates:
            parts.append(f"{self.skipped_duplicates} already present")
        parts.append(f"{self.activated} activated" if self.activated else "0 activated")
        return ", ".join(parts)


def seed_profile(
    repository: ProfileRepository,
    *,
    path: str | Path = SEED_PATH,
    activate: bool = False,
    now: datetime | None = None,
) -> SeedResult:
    """Load the starter statements into the store.

    Idempotent on statement text, so running it twice does not produce fifty
    duplicates. `activate` exists for the case where the operator has read the
    file and accepts all of it; the default leaves everything for review.
    """
    seed_file = SeedFile.load(path)
    existing = {statement.statement.strip().lower() for statement in repository.all()}
    result = SeedResult()

    for entry in seed_file.statements:
        if entry.statement.strip().lower() in existing:
            result.skipped_duplicates += 1
            continue
        statement = entry.to_statement(now=now)
        if activate:
            statement = statement.model_copy(update={"status": StatementStatus.ACTIVE})
            result.activated += 1
        repository.add(statement)
        result.loaded += 1

    log.info(
        "seeded profile",
        extra={
            "loaded": result.loaded,
            "skipped": result.skipped_duplicates,
            "activated": result.activated,
        },
    )
    return result
