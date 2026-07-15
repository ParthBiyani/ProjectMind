"""Hybrid retrieval: lexical and vector, fused, then cut.

Two retrievers disagree usefully. BM25 finds `num_workers` and `CTC`; the
embedding finds "the held-out score collapsed" when the record says "scores
finally agreed". Reciprocal rank fusion merges them without needing their
scores to be on the same scale, which they are not and never will be.

**The cut is the part that matters.** Fusion decides the order; it does not
decide how many to serve, and serving five records for a question with one
answer is four wrong answers. Measured on the eval set, an untrimmed top-5
produced a false injection rate of 0.707 against a target of 0.10. Three rules
fix that, in this order:

1. an absolute floor, below which a record is not evidence of anything;
2. a **relative** floor — a record must be within a fraction of the best hit,
   so a query with one strong answer serves one record and a query with four
   comparable answers serves four;
3. the cap, which is the least interesting of the three and almost never binds.

Recall is deliberately sacrificed. An empty answer costs nothing; a confident
wrong one costs the reader's trust in every future answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from projectmind.config import Settings, get_settings
from projectmind.gate.rules import GateDecision
from projectmind.logging import get_logger
from projectmind.models import EpisodicRecord, Fingerprint, Project, ServedRecord, TaskType
from projectmind.retrieval.lexical import BM25Index
from projectmind.retrieval.ranking import (
    Candidate,
    RankingWeights,
    ScoredCandidate,
    rank,
    to_served,
)
from projectmind.storage import RecordFilter, Store
from projectmind.storage.embeddings import EmbeddingProvider, get_embedder

log = get_logger(__name__)

#: Reciprocal rank fusion constant, used only as a tie-break.
#:
#: Pure RRF was tried first and does not work at this scale. With k=60 over a
#: fifty-record corpus, 1/(60+1) and 1/(60+10) differ by 13%, so the tenth-best
#: result scored 0.87 of the best and the relative floor below cut nothing:
#: false injection stayed at 0.676 against a target of 0.10. RRF assumes result
#: sets far larger than a single developer's history. The normalised scores
#: discriminate properly, so they lead and RRF only breaks ties.
RRF_K = 10

#: How many candidates each retriever contributes before fusion.
PER_RETRIEVER = 30

#: Absolute floor on the final ranked score.
#:
#: This and RELATIVE_FLOOR were fitted on the 40-query eval set by sweeping
#: them against the cross-project boost and the fingerprint floor; see
#: docs/evaluation.md. The set is therefore no longer a clean held-out measure
#: of *these four constants*, which is stated rather than hidden.
MINIMUM_SCORE = 0.38

#: A record must score at least this fraction of the best hit to be served.
#: This is what makes the number of results depend on the question rather than
#: on the cap: a query with one strong answer serves one record, a query with
#: four comparable answers serves four.
RELATIVE_FLOOR = 0.80

#: Fusion weights. Lexical leads because exact technical tokens are the most
#: reliable signal in this corpus, and because the default embedder is a
#: hashing fallback rather than a trained encoder.
LEXICAL_WEIGHT = 0.6
VECTOR_WEIGHT = 0.4

#: How much the rank-based tie-break contributes. Small on purpose: it exists
#: to order records the two scores rate equally, not to move them.
TIE_BREAK_WEIGHT = 0.1


@dataclass(slots=True)
class Fusion:
    """The per-record output of fusing the two rankings."""

    relevance: float = 0.0
    lexical: float = 0.0
    vector: float = 0.0
    rrf: float = 0.0
    sources: set[str] = field(default_factory=set)


class HybridRetriever:
    """BM25 and vector search, fused, ranked and trimmed."""

    name = "hybrid"

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.weights = RankingWeights.from_settings(self.settings)
        self._embedder = embedder

    @property
    def embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    def retrieve(
        self,
        decision: GateDecision,
        *,
        project: Project | None,
        fingerprint: Fingerprint,
        limit: int,
        now: datetime | None = None,
    ) -> list[ServedRecord]:
        pool = self._pool(decision)
        if not pool:
            return []

        query = decision.analysis.prompt
        fused = self._fuse(pool, query)
        if not fused:
            return []

        projects = {item.id: item for item in self.store.list_projects()}
        caller_id = project.id if project else None
        entity_filter = frozenset(decision.entity_filter)
        by_id = {record.id: record for record in pool}

        candidates = [
            Candidate(
                record=by_id[record_id],
                project=projects.get(by_id[record_id].project_id),
                lexical_score=detail.lexical,
                vector_score=detail.vector,
                matched_entities=tuple(
                    sorted(entity_filter & frozenset(by_id[record_id].entities))
                ),
                sources=set(detail.sources),
            )
            for record_id, detail in fused.items()
        ]

        weights = self._weights_for(decision)
        ranked = rank(
            candidates,
            caller_project_id=caller_id,
            caller_fingerprint=fingerprint,
            weights=weights,
            now=now,
            relevance={
                candidate.record.id: fused[candidate.record.id].relevance
                for candidate in candidates
            },
            minimum_score=MINIMUM_SCORE,
        )
        kept = self._cut(ranked, limit)

        log.debug(
            "hybrid retrieval",
            extra={
                "pool": len(pool),
                "fused": len(fused),
                "above_floor": len(ranked),
                "served": len(kept),
            },
        )
        return [to_served(item, caller_project_id=caller_id) for item in kept]

    # --- stages ------------------------------------------------------------
    def _pool(self, decision: GateDecision) -> list[EpisodicRecord]:
        """Everything the gate permits. Filtering happens before scoring."""
        floor = self.settings.episodic_serve_confidence
        records = self.store.list_records(
            RecordFilter(types=decision.record_types, min_confidence=floor, limit=500)
        )
        if not decision.entity_filter:
            return records

        named = frozenset(decision.entity_filter)
        narrowed = [record for record in records if named & frozenset(record.entities)]
        if narrowed:
            return narrowed
        # An entity-named prompt that matches no record only widens for task
        # types that would have searched anyway.
        if decision.task_type in {TaskType.ARCHITECT, TaskType.EXPLORE, TaskType.DEBUG}:
            return records
        return []

    def _fuse(self, pool: Sequence[EpisodicRecord], query: str) -> dict[UUID, Fusion]:
        """Blend the two rankings into one relevance score in 0..1.

        Both inputs are normalised against their own best hit first, which is
        what makes a BM25 score and a cosine comparable: neither absolute value
        means anything, but "90% as good as the best lexical match" and "90% as
        good as the best semantic match" do.
        """
        lexical = BM25Index.build(pool).search(query, limit=PER_RETRIEVER)
        vector = self._vector_search(pool, query, limit=PER_RETRIEVER)

        best_vector = max((score for _, score in vector), default=0.0)
        fused: dict[UUID, Fusion] = {}

        for position, hit in enumerate(lexical, start=1):
            entry = fused.setdefault(hit.record_id, Fusion())
            entry.lexical = hit.score
            entry.rrf += LEXICAL_WEIGHT / (RRF_K + position)
            entry.sources.add("bm25")

        for position, (record_id, similarity) in enumerate(vector, start=1):
            entry = fused.setdefault(record_id, Fusion())
            entry.vector = similarity / best_vector if best_vector else 0.0
            entry.rrf += VECTOR_WEIGHT / (RRF_K + position)
            entry.sources.add("vector")

        if not fused:
            return {}

        best_rrf = max(entry.rrf for entry in fused.values()) or 1.0
        for entry in fused.values():
            blended = LEXICAL_WEIGHT * entry.lexical + VECTOR_WEIGHT * entry.vector
            entry.relevance = blended + TIE_BREAK_WEIGHT * (entry.rrf / best_rrf)

        best = max(entry.relevance for entry in fused.values()) or 1.0
        for entry in fused.values():
            entry.relevance = min(1.0, entry.relevance / best)
        return fused

    def _vector_search(
        self, pool: Sequence[EpisodicRecord], query: str, *, limit: int
    ) -> list[tuple[UUID, float]]:
        """Cosine similarity, delegated to the store.

        This has to go through `Store.vector_search` rather than scoring the
        pool in process. `list_records` deliberately does not load embeddings —
        dragging kilobytes of float through memory for a lexical query would be
        waste — so the in-process version silently scored every record at zero
        and the "hybrid" retriever was lexical-only. The store joins the
        embedding side table, and on Postgres does the whole thing in the index.
        """
        try:
            embedded = self.embedder.embed([query])[0]
        except Exception as exc:
            log.warning("query embedding failed, lexical only", extra={"error": str(exc)})
            return []

        allowed = {record.id for record in pool}
        scored = [
            (hit.record.id, hit.similarity)
            for hit in self.store.vector_search(embedded, limit=limit * 4)
            if hit.record.id in allowed and hit.similarity > 0.0
        ]
        return scored[:limit]

    def _weights_for(self, decision: GateDecision) -> RankingWeights:
        if decision.cross_project_boost == self.weights.cross_project_boost:
            return self.weights
        return RankingWeights(
            recency_half_life_days=self.weights.recency_half_life_days,
            fingerprint_weight=self.weights.fingerprint_weight,
            cross_project_boost=decision.cross_project_boost,
            lexical_weight=self.weights.lexical_weight,
            entity_bonus=self.weights.entity_bonus,
        )

    @staticmethod
    def _cut(ranked: list[ScoredCandidate], limit: int) -> list[ScoredCandidate]:
        """Relative floor, then the cap.

        The relative floor is what lets one query serve one record and another
        serve four, without either being told in advance how many to expect.
        """
        if not ranked:
            return []
        threshold = ranked[0].score * RELATIVE_FLOOR
        strong = [item for item in ranked if item.score >= threshold]
        return strong[:limit]
