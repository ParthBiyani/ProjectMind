"""The storage contract.

Two backends implement this: SQLite, which is the default so that the server is
available without a daemon, and Postgres with pgvector, selected by setting a
`postgresql://` URL. No caller is allowed to reach past this interface, and a
single parity suite runs against both so they cannot quietly diverge.

The store is deliberately dumb. It stores, filters and does vector similarity;
ranking, fusion and gating live in `retrieval/` and `gate/` where they can be
tested without a database.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from projectmind.models import (
    Category,
    EpisodicRecord,
    EvidenceRef,
    Fingerprint,
    Origin,
    ProfileStatement,
    Project,
    RecordType,
    Resolution,
    ReviewItem,
    ReviewKind,
    SourceType,
    StatementStatus,
)

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Query filters
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecordFilter:
    """Filters for an episodic query. Every field is optional and ANDed."""

    project_ids: tuple[UUID, ...] = ()
    exclude_project_ids: tuple[UUID, ...] = ()
    types: tuple[RecordType, ...] = ()
    entities: tuple[str, ...] = ()
    min_confidence: float = 0.0
    occurred_after: datetime | None = None
    include_superseded: bool = False
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class StatementFilter:
    statuses: tuple[StatementStatus, ...] = (StatementStatus.ACTIVE,)
    categories: tuple[Category, ...] = ()
    entities: tuple[str, ...] = ()
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class ScoredRecord:
    """A record with the raw similarity the backend produced for it."""

    record: EpisodicRecord
    similarity: float


@dataclass(slots=True)
class BundleLogEntry:
    """One `get_context` call, recorded so the gate can be scored offline.

    The prompt itself is never stored, only a hash of it. The point is to
    measure retrieval behaviour, not to keep a transcript of the user's work.
    """

    bundle_id: UUID
    created_at: datetime
    project_key: str | None
    task_type: str | None
    prompt_hash: str
    prompt_words: int
    profile_ids: tuple[UUID, ...] = ()
    episodic_ids: tuple[UUID, ...] = ()
    cross_project_ids: tuple[UUID, ...] = ()
    profile_tokens: int = 0
    episodic_tokens: int = 0
    gate_reason: str = ""
    latency_ms: float = 0.0
    injected: bool = False
    feedback: bool | None = None
    feedback_note: str = ""


@dataclass(slots=True)
class UsageStats:
    """Aggregates behind `projectmind report`."""

    bundles: int = 0
    injected: int = 0
    skipped: int = 0
    with_episodic: int = 0
    cross_project_bundles: int = 0
    total_profile_tokens: int = 0
    total_episodic_tokens: int = 0
    feedback_positive: int = 0
    feedback_negative: int = 0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    by_task_type: dict[str, int] = field(default_factory=dict)
    by_project: dict[str, int] = field(default_factory=dict)
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def injection_rate(self) -> float:
        return self.injected / self.bundles if self.bundles else 0.0

    @property
    def observed_false_injection_rate(self) -> float:
        """Share of rated bundles the user marked unhelpful.

        This is the live counterpart to the offline `false_injection_rate`. It
        only covers bundles that were actually rated, which is always a subset.
        """
        rated = self.feedback_positive + self.feedback_negative
        return self.feedback_negative / rated if rated else 0.0


# --------------------------------------------------------------------------- #
# Row codecs, shared by both backends
# --------------------------------------------------------------------------- #


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def from_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def from_iso_opt(value: Any) -> datetime | None:
    return None if value in (None, "") else from_iso(value)


def project_to_row(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "key": project.key,
        "name": project.name,
        "root_path": project.root_path,
        "git_remote": project.git_remote,
        "fingerprint": dumps(project.fingerprint.model_dump(mode="json")),
        "first_seen": to_iso(project.first_seen),
        "last_active": to_iso(project.last_active),
    }


def project_from_row(row: Any) -> Project:
    return Project(
        id=UUID(str(row["id"])),
        key=row["key"],
        name=row["name"],
        root_path=row["root_path"],
        git_remote=row["git_remote"],
        fingerprint=Fingerprint.model_validate(loads(row["fingerprint"], {})),
        first_seen=from_iso(row["first_seen"]),
        last_active=from_iso(row["last_active"]),
    )


def statement_to_row(statement: ProfileStatement) -> dict[str, Any]:
    return {
        "id": str(statement.id),
        "statement": statement.statement,
        "category": str(statement.category),
        "scope": dumps(statement.scope) if statement.scope else None,
        "evidence_refs": dumps([ref.model_dump(mode="json") for ref in statement.evidence_refs]),
        "confidence": statement.confidence,
        "status": str(statement.status),
        "superseded_by": str(statement.superseded_by) if statement.superseded_by else None,
        "supersedes": str(statement.supersedes) if statement.supersedes else None,
        "contradiction_count": statement.contradiction_count,
        "contradiction_refs": dumps(list(statement.contradiction_refs)),
        "entities": dumps(list(statement.entities)),
        "created_at": to_iso(statement.created_at),
        "last_confirmed_at": to_iso(statement.last_confirmed_at),
        "ttl_days": statement.ttl_days,
        "origin": str(statement.origin),
    }


def statement_from_row(row: Any) -> ProfileStatement:
    return ProfileStatement(
        id=UUID(str(row["id"])),
        statement=row["statement"],
        category=Category(row["category"]),
        scope=loads(row["scope"], None),
        evidence_refs=tuple(
            EvidenceRef.model_validate(ref) for ref in loads(row["evidence_refs"], [])
        ),
        confidence=float(row["confidence"]),
        status=StatementStatus(row["status"]),
        superseded_by=UUID(str(row["superseded_by"])) if row["superseded_by"] else None,
        supersedes=UUID(str(row["supersedes"])) if row["supersedes"] else None,
        contradiction_count=int(row["contradiction_count"]),
        contradiction_refs=tuple(loads(row["contradiction_refs"], [])),
        entities=tuple(loads(row["entities"], [])),
        created_at=from_iso(row["created_at"]),
        last_confirmed_at=from_iso(row["last_confirmed_at"]),
        ttl_days=int(row["ttl_days"]),
        origin=Origin(row["origin"]),
    )


def record_to_row(record: EpisodicRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "project_id": str(record.project_id),
        "type": str(record.type),
        "what": record.what,
        "why": record.why,
        "rejected_alternatives": dumps(list(record.rejected_alternatives)),
        "outcome": record.outcome,
        "entities": dumps(list(record.entities)),
        "source_type": str(record.source_type),
        "source_url": record.source_url,
        "occurred_at": to_iso(record.occurred_at),
        "confidence": record.confidence,
        "is_inference": int(record.is_inference),
        "superseded_by": str(record.superseded_by) if record.superseded_by else None,
        "content_hash": record.content_hash,
        "ingested_at": to_iso(record.ingested_at),
    }


def record_from_row(row: Any, embedding: Sequence[float] | None = None) -> EpisodicRecord:
    return EpisodicRecord(
        id=UUID(str(row["id"])),
        project_id=UUID(str(row["project_id"])),
        type=RecordType(row["type"]),
        what=row["what"],
        why=row["why"] or "",
        rejected_alternatives=tuple(loads(row["rejected_alternatives"], [])),
        outcome=row["outcome"] or "",
        entities=tuple(loads(row["entities"], [])),
        source_type=SourceType(row["source_type"]),
        source_url=row["source_url"],
        occurred_at=from_iso(row["occurred_at"]),
        confidence=float(row["confidence"]),
        is_inference=bool(row["is_inference"]),
        superseded_by=UUID(str(row["superseded_by"])) if row["superseded_by"] else None,
        embedding=tuple(embedding) if embedding is not None else None,
        content_hash=row["content_hash"],
        ingested_at=from_iso(row["ingested_at"]),
    )


def review_to_row(item: ReviewItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "kind": str(item.kind),
        "target_id": str(item.target_id),
        "proposal": item.proposal,
        "rationale": item.rationale,
        "evidence_refs": dumps([ref.model_dump(mode="json") for ref in item.evidence_refs]),
        "impact": item.impact,
        "created_at": to_iso(item.created_at),
        "resolved_at": to_iso(item.resolved_at),
        "resolution": str(item.resolution) if item.resolution else None,
        "resolution_note": item.resolution_note,
        "batch_id": str(item.batch_id) if item.batch_id else None,
    }


def review_from_row(row: Any) -> ReviewItem:
    return ReviewItem(
        id=UUID(str(row["id"])),
        kind=ReviewKind(row["kind"]),
        target_id=UUID(str(row["target_id"])),
        proposal=row["proposal"],
        rationale=row["rationale"] or "",
        evidence_refs=tuple(
            EvidenceRef.model_validate(ref) for ref in loads(row["evidence_refs"], [])
        ),
        impact=float(row["impact"]),
        created_at=from_iso(row["created_at"]),
        resolved_at=from_iso_opt(row["resolved_at"]),
        resolution=Resolution(row["resolution"]) if row["resolution"] else None,
        resolution_note=row["resolution_note"] or "",
        batch_id=UUID(str(row["batch_id"])) if row["batch_id"] else None,
    )


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #


class Store(ABC):
    """Persistence for every layer of memory."""

    dialect: str

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def migrate(self) -> int:
        """Bring the schema up to date. Returns the version now in place."""

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- projects ----------------------------------------------------------
    @abstractmethod
    def upsert_project(self, project: Project) -> Project: ...

    @abstractmethod
    def get_project(self, key: str) -> Project | None: ...

    @abstractmethod
    def get_project_by_id(self, project_id: UUID) -> Project | None: ...

    @abstractmethod
    def list_projects(self) -> list[Project]: ...

    # --- profile -----------------------------------------------------------
    @abstractmethod
    def upsert_statement(self, statement: ProfileStatement) -> ProfileStatement: ...

    @abstractmethod
    def get_statement(self, statement_id: UUID) -> ProfileStatement | None: ...

    @abstractmethod
    def list_statements(self, filters: StatementFilter | None = None) -> list[ProfileStatement]: ...

    @abstractmethod
    def delete_statement(self, statement_id: UUID) -> bool: ...

    # --- episodic ----------------------------------------------------------
    @abstractmethod
    def add_records(self, records: Iterable[EpisodicRecord]) -> int:
        """Insert records, skipping any whose `content_hash` already exists."""

    @abstractmethod
    def get_record(self, record_id: UUID) -> EpisodicRecord | None: ...

    @abstractmethod
    def list_records(self, filters: RecordFilter | None = None) -> list[EpisodicRecord]: ...

    @abstractmethod
    def count_records(self) -> int: ...

    @abstractmethod
    def vector_search(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 20,
        filters: RecordFilter | None = None,
    ) -> list[ScoredRecord]:
        """Nearest neighbours by cosine similarity, honouring the same filters."""

    # --- review queue ------------------------------------------------------
    @abstractmethod
    def enqueue_review(self, item: ReviewItem) -> ReviewItem: ...

    @abstractmethod
    def list_reviews(self, *, open_only: bool = True) -> list[ReviewItem]: ...

    @abstractmethod
    def resolve_review(
        self,
        review_id: UUID,
        resolution: Resolution,
        *,
        note: str = "",
        resolved_at: datetime | None = None,
    ) -> ReviewItem | None: ...

    # --- fingerprint cache -------------------------------------------------
    @abstractmethod
    def get_cached_fingerprint(self, project_key: str) -> tuple[str, Fingerprint] | None:
        """Returns (manifest_hash, fingerprint) or None when nothing is cached."""

    @abstractmethod
    def put_cached_fingerprint(self, project_key: str, fingerprint: Fingerprint) -> None: ...

    @abstractmethod
    def invalidate_fingerprint(self, project_key: str) -> None: ...

    # --- telemetry ---------------------------------------------------------
    @abstractmethod
    def log_bundle(self, entry: BundleLogEntry) -> None: ...

    @abstractmethod
    def record_feedback(self, bundle_id: UUID, *, useful: bool, note: str = "") -> bool: ...

    @abstractmethod
    def recent_bundles(self, limit: int = 50) -> list[BundleLogEntry]: ...

    @abstractmethod
    def usage_stats(self, *, since: datetime | None = None) -> UsageStats: ...


def aggregate_usage(entries: Iterable[BundleLogEntry]) -> UsageStats:
    """Shared by both backends so the numbers cannot drift between them."""
    stats = UsageStats()
    latencies: list[float] = []
    for entry in entries:
        stats.bundles += 1
        if entry.injected:
            stats.injected += 1
        else:
            stats.skipped += 1
        if entry.episodic_ids:
            stats.with_episodic += 1
        if entry.cross_project_ids:
            stats.cross_project_bundles += 1
        stats.total_profile_tokens += entry.profile_tokens
        stats.total_episodic_tokens += entry.episodic_tokens
        if entry.feedback is True:
            stats.feedback_positive += 1
        elif entry.feedback is False:
            stats.feedback_negative += 1
        if entry.task_type:
            stats.by_task_type[entry.task_type] = stats.by_task_type.get(entry.task_type, 0) + 1
        if entry.project_key:
            stats.by_project[entry.project_key] = stats.by_project.get(entry.project_key, 0) + 1
        latencies.append(entry.latency_ms)
        stats.first_seen = min(stats.first_seen or entry.created_at, entry.created_at)
        stats.last_seen = max(stats.last_seen or entry.created_at, entry.created_at)
    if latencies:
        ordered = sorted(latencies)
        stats.mean_latency_ms = sum(ordered) / len(ordered)
        stats.p95_latency_ms = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return stats
