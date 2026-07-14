"""Ranking.

Four factors decide the order, and one of them is the whole product:

    score = confidence x recency x (floor + weight x fingerprint similarity) x boost

The **cross-project boost** is the last term. Without it a mediocre result from
the repository you are sitting in outranks a strong one from a repository you
worked in eight months ago, and the system has quietly rebuilt a per-repo index
— the exact thing it exists not to be. With it, a same-repo result has to be
genuinely better to win.

The boost is a multiplier rather than an additive bonus so that it scales what
is already there instead of promoting weak matches. A cross-project record with
nothing going for it stays at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from projectmind.config import Settings, get_settings
from projectmind.models import EpisodicRecord, Fingerprint, Project, ServedRecord, utcnow

#: Floor on the fingerprint term. A record from a project with no measurable
#: similarity is worth less, not worthless: the strongest cross-project lesson
#: in a portfolio is often "this bit me in a completely different stack".
FINGERPRINT_FLOOR = 0.45


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """Tunable ranking parameters, resolved from settings."""

    recency_half_life_days: float = 240.0
    fingerprint_weight: float = 0.55
    cross_project_boost: float = 1.25
    lexical_weight: float = 0.5
    entity_bonus: float = 0.15

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> RankingWeights:
        settings = settings or get_settings()
        return cls(
            recency_half_life_days=settings.recency_half_life_days,
            fingerprint_weight=settings.fingerprint_weight,
            cross_project_boost=settings.cross_project_boost,
            lexical_weight=settings.lexical_weight,
        )


@dataclass(slots=True)
class Candidate:
    """A record on its way to being ranked, with whatever signals were gathered."""

    record: EpisodicRecord
    project: Project | None = None
    lexical_score: float = 0.0
    vector_score: float = 0.0
    matched_entities: tuple[str, ...] = ()
    sources: set[str] = field(default_factory=set)

    @property
    def project_key(self) -> str:
        return self.project.key if self.project else "unknown"

    def fingerprint_similarity(self, target: Fingerprint | None) -> float:
        if target is None or self.project is None:
            return 0.0
        return target.similarity(self.project.fingerprint)


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A ranked record and the arithmetic that got it there."""

    candidate: Candidate
    score: float
    recency: float
    fingerprint: float
    boosted: bool
    reason: str

    @property
    def record(self) -> EpisodicRecord:
        return self.candidate.record

    @property
    def project_key(self) -> str:
        return self.candidate.project_key


def score_candidate(
    candidate: Candidate,
    *,
    caller_project_id: UUID | None,
    caller_fingerprint: Fingerprint | None,
    weights: RankingWeights,
    now: datetime | None = None,
    relevance: float | None = None,
) -> ScoredCandidate:
    """Score one candidate.

    `relevance` is the retriever's own similarity signal, whatever produced it.
    When it is None the record's stored confidence carries the whole weight,
    which is the right behaviour for a pure entity-filtered lookup.
    """
    moment = now or utcnow()
    record = candidate.record

    base = record.confidence if relevance is None else (record.confidence + relevance) / 2.0
    recency = record.recency_weight(moment, half_life_days=weights.recency_half_life_days)
    similarity = candidate.fingerprint_similarity(caller_fingerprint)
    fingerprint_term = FINGERPRINT_FLOOR + weights.fingerprint_weight * similarity

    cross_project = caller_project_id is not None and record.project_id != caller_project_id
    boost = weights.cross_project_boost if cross_project else 1.0

    total = base * recency * fingerprint_term * boost
    if candidate.matched_entities:
        total *= 1.0 + weights.entity_bonus

    parts = [f"confidence {record.confidence:.2f}", f"recency {recency:.2f}"]
    if similarity:
        parts.append(f"fingerprint {similarity:.2f}")
    if cross_project:
        parts.append(f"cross-project x{boost:.2f}")
    if candidate.matched_entities:
        parts.append("names " + ", ".join(candidate.matched_entities[:3]))

    return ScoredCandidate(
        candidate=candidate,
        score=round(total, 6),
        recency=round(recency, 4),
        fingerprint=round(similarity, 4),
        boosted=cross_project,
        reason="; ".join(parts),
    )


def rank(
    candidates: list[Candidate],
    *,
    caller_project_id: UUID | None,
    caller_fingerprint: Fingerprint | None,
    weights: RankingWeights | None = None,
    now: datetime | None = None,
    relevance: dict[UUID, float] | None = None,
    minimum_score: float = 0.0,
) -> list[ScoredCandidate]:
    """Score and order candidates, best first."""
    weights = weights or RankingWeights.from_settings()
    relevance = relevance or {}
    scored = [
        score_candidate(
            candidate,
            caller_project_id=caller_project_id,
            caller_fingerprint=caller_fingerprint,
            weights=weights,
            now=now,
            relevance=relevance.get(candidate.record.id),
        )
        for candidate in candidates
    ]
    kept = [item for item in scored if item.score >= minimum_score]
    kept.sort(key=lambda item: (-item.score, item.record.occurred_at.timestamp() * -1))
    return kept


def to_served(scored: ScoredCandidate, *, caller_project_id: UUID | None) -> ServedRecord:
    """Convert to the shape the agent sees, provenance intact."""
    record = scored.record
    return ServedRecord(
        id=record.id,
        project_key=scored.project_key,
        type=record.type,
        what=record.what,
        why=record.why,
        outcome=record.outcome,
        occurred_at=record.occurred_at,
        source_url=record.source_url,
        confidence=record.confidence,
        is_inference=record.is_inference,
        score=scored.score,
        cross_project=caller_project_id is not None and record.project_id != caller_project_id,
    )
