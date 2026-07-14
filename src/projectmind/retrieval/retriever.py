"""Fetching episodic candidates and ordering them.

The baseline retriever: filter by what the gate allows, rank by the formula in
`ranking.py`, stop. No text search yet — that is Phase 4, and adding it before
this is measured would make it impossible to say which part helped.

One rule governs the whole module: **empty is a valid answer.** Every widening
step below is conditional, and none of them fire just because the previous one
returned nothing to show.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from projectmind.config import Settings, get_settings
from projectmind.gate.rules import GateDecision
from projectmind.logging import get_logger
from projectmind.models import EpisodicRecord, Fingerprint, Project, ServedRecord, TaskType
from projectmind.retrieval.ranking import Candidate, RankingWeights, rank, to_served
from projectmind.storage import RecordFilter, Store

log = get_logger(__name__)

#: How many candidates to pull before ranking. Ranking is cheap; the store
#: query is the expensive part, so over-fetching a little beats re-querying.
CANDIDATE_POOL = 200

#: A ranked record below this contributes nothing but tokens.
MINIMUM_SCORE = 0.25


class BaselineRetriever:
    """Entity-filtered, fingerprint-weighted retrieval over episodic memory."""

    name = "baseline"

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.weights = RankingWeights.from_settings(self.settings)

    def retrieve(
        self,
        decision: GateDecision,
        *,
        project: Project | None,
        fingerprint: Fingerprint,
        limit: int,
        now: datetime | None = None,
    ) -> list[ServedRecord]:
        """Candidates the gate allows, ranked, trimmed to `limit`."""
        candidates = self._candidates(decision)
        if not candidates:
            log.debug("no episodic candidates", extra={"reason": decision.reason})
            return []

        projects = {item.id: item for item in self.store.list_projects()}
        caller_id = project.id if project else None
        entity_filter = frozenset(decision.entity_filter)

        pool = [
            Candidate(
                record=record,
                project=projects.get(record.project_id),
                matched_entities=tuple(sorted(entity_filter & frozenset(record.entities))),
                sources={"filter"},
            )
            for record in candidates
        ]

        weights = self.weights
        if decision.cross_project_boost != weights.cross_project_boost:
            weights = RankingWeights(
                recency_half_life_days=weights.recency_half_life_days,
                fingerprint_weight=weights.fingerprint_weight,
                cross_project_boost=decision.cross_project_boost,
                lexical_weight=weights.lexical_weight,
                entity_bonus=weights.entity_bonus,
            )

        ranked = rank(
            pool,
            caller_project_id=caller_id,
            caller_fingerprint=fingerprint,
            weights=weights,
            now=now,
            minimum_score=MINIMUM_SCORE,
        )
        served = [to_served(item, caller_project_id=caller_id) for item in ranked[:limit]]
        log.debug(
            "retrieved episodic records",
            extra={
                "candidates": len(pool),
                "ranked": len(ranked),
                "served": len(served),
                "cross_project": sum(1 for item in served if item.cross_project),
            },
        )
        return served

    # --- candidate selection ----------------------------------------------
    def _candidates(self, decision: GateDecision) -> list[EpisodicRecord]:
        """Fetch the pool the gate's decision permits.

        Entity-filtered first, because a named entity is the strongest evidence
        there is something worth finding. The widening fallback only applies to
        task types that asked to search without naming anything; for an
        entity-named prompt that found nothing, nothing is the answer.
        """
        floor = self.settings.episodic_serve_confidence
        types = decision.record_types

        if decision.entity_filter:
            narrow = self.store.list_records(
                RecordFilter(
                    types=types,
                    entities=decision.entity_filter,
                    min_confidence=floor,
                    limit=CANDIDATE_POOL,
                )
            )
            if narrow:
                return narrow
            if decision.task_type not in {TaskType.ARCHITECT, TaskType.EXPLORE, TaskType.DEBUG}:
                return []

        return self.store.list_records(
            RecordFilter(types=types, min_confidence=floor, limit=CANDIDATE_POOL)
        )


def search(
    store: Store,
    query: str,
    *,
    settings: Settings | None = None,
    project_key: str | None = None,
    types: Sequence[str] = (),
    limit: int = 10,
) -> list[ServedRecord]:
    """Free-text search, for `search_memory` and the CLI.

    Deliberately separate from `retrieve`: this is a human asking a direct
    question, not the gate deciding what to push. Precision still matters but
    an empty answer is more annoying here, so the confidence floor is the only
    filter applied.
    """
    from projectmind.entities import build_vocabulary, extract
    from projectmind.models import RecordType

    settings = settings or get_settings()
    projects = {item.id: item for item in store.list_projects()}
    caller: UUID | None = None
    caller_fingerprint: Fingerprint | None = None
    if project_key:
        found = store.get_project(project_key)
        if found:
            caller = found.id
            caller_fingerprint = found.fingerprint

    record_types = tuple(RecordType(value) for value in types)
    pool = store.list_records(
        RecordFilter(
            types=record_types,
            min_confidence=settings.episodic_serve_confidence,
            limit=CANDIDATE_POOL * 2,
        )
    )
    vocabulary = build_vocabulary(*(record.entities for record in pool))
    named = frozenset(extract(query, vocabulary))

    terms = {term for term in query.lower().split() if len(term) > 2}
    candidates: list[Candidate] = []
    relevance: dict[UUID, float] = {}
    for record in pool:
        text = record.searchable_text.lower()
        overlap = sum(1 for term in terms if term in text)
        matched = tuple(sorted(named & frozenset(record.entities)))
        if not overlap and not matched:
            continue
        relevance[record.id] = min(1.0, (overlap / max(1, len(terms))) + 0.3 * len(matched))
        candidates.append(
            Candidate(
                record=record,
                project=projects.get(record.project_id),
                matched_entities=matched,
                sources={"search"},
            )
        )

    ranked = rank(
        candidates,
        caller_project_id=caller,
        caller_fingerprint=caller_fingerprint,
        relevance=relevance,
        minimum_score=MINIMUM_SCORE,
    )
    return [to_served(item, caller_project_id=caller) for item in ranked[:limit]]
