"""The reflection loop, as a state machine.

    load -> detect contradictions -> open proposals
         -> scan TTLs -> refresh passively -> queue the rest
         -> batch

Built on LangGraph when it is installed, and on a plain sequential executor
when it is not. That is not hedging: the graph is five nodes with no branching
and no cycles, so a loop over them is exactly equivalent. LangGraph earns its
place through checkpointing and inspectability — being able to see which node a
monthly run died in, and resume it — not through orchestration this pipeline
needs. Making it optional keeps `projectmind review` working on a machine that
never installed it.

Both paths run the *same node functions*. There is no second implementation to
drift.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from projectmind.config import Settings, get_settings
from projectmind.logging import get_logger
from projectmind.models import (
    EpisodicRecord,
    EvidenceRef,
    ProfileStatement,
    ReviewItem,
    ReviewKind,
    SourceType,
    utcnow,
)
from projectmind.profile.lifecycle import evaluate_contradictions
from projectmind.profile.repository import ProfileRepository
from projectmind.reflection import contradiction as contradiction_rules
from projectmind.reflection import ttl as ttl_rules
from projectmind.storage import RecordFilter, Store

log = get_logger(__name__)

#: How far back a run looks for evidence. Wider than the contradiction window
#: on purpose: passive re-confirmation should see everything since a statement
#: was last confirmed, which can be a year for an abandonment.
DEFAULT_LOOKBACK_DAYS = 400


@dataclass
class ReflectionState:
    """Everything a run carries between nodes.

    A plain dataclass rather than a TypedDict so the same object works whether
    LangGraph is driving or the fallback loop is.
    """

    now: datetime = field(default_factory=utcnow)
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    batch_id: UUID = field(default_factory=uuid4)

    statements: list[ProfileStatement] = field(default_factory=list)
    records: list[EpisodicRecord] = field(default_factory=list)

    contradictions: dict[str, list[contradiction_rules.Contradiction]] = field(default_factory=dict)
    detection: contradiction_rules.DetectionStats = field(
        default_factory=contradiction_rules.DetectionStats
    )
    proposals: list[contradiction_rules.Proposal] = field(default_factory=list)
    ttl: ttl_rules.TtlScan = field(default_factory=ttl_rules.TtlScan)

    queued: list[ReviewItem] = field(default_factory=list)
    refreshed: int = 0
    dormant: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.statements)} statements, {len(self.records)} records, "
            f"{len(self.proposals)} supersession proposals, "
            f"{self.refreshed} refreshed silently, "
            f"{len(self.queued)} items queued for review"
        )


class ReflectionLoop:
    """Runs the loop. Writes only through the repository and the store."""

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        *,
        profile: ProfileRepository | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.profile = profile or ProfileRepository(store, self.settings)

    # --- nodes -------------------------------------------------------------
    def load(self, state: ReflectionState) -> ReflectionState:
        """Read the active profile and the episodic window."""
        state.statements = self.profile.active()
        since = state.now - timedelta(days=state.lookback_days)
        state.records = self.store.list_records(
            RecordFilter(occurred_after=since, min_confidence=0.0, limit=5_000)
        )
        log.info(
            "reflection loaded",
            extra={"statements": len(state.statements), "records": len(state.records)},
        )
        return state

    def detect_contradictions(self, state: ReflectionState) -> ReflectionState:
        state.contradictions, state.detection = contradiction_rules.detect(
            state.statements, state.records
        )
        return state

    def open_proposals(self, state: ReflectionState) -> ReflectionState:
        """Turn contradictions into proposals, but only past the threshold.

        One contradicting record never opens a proposal. Three inside the
        window does, and so does a single explicit reversal — the rule lives in
        `profile.lifecycle` so that the threshold is stated once.
        """
        by_id = {str(statement.id): statement for statement in state.statements}
        for statement_id, evidence in state.contradictions.items():
            statement = by_id.get(statement_id)
            if statement is None:
                continue
            verdict = evaluate_contradictions(
                statement,
                [item.record for item in evidence],
                now=state.now,
                window_days=self.settings.contradiction_window_days,
                threshold=self.settings.contradiction_threshold,
            )
            if not verdict:
                log.debug(
                    "contradiction held below threshold",
                    extra={"statement": statement_id, "reason": verdict.reason},
                )
                continue

            recent = [item for item in evidence if item.record.id in set(verdict.supporting)]
            proposal = contradiction_rules.propose(statement, recent or evidence)
            state.proposals.append(proposal)

            for item in recent or evidence:
                self.profile.note_contradiction(statement.id, item.record)

            state.queued.append(
                ReviewItem(
                    kind=ReviewKind.SUPERSESSION,
                    target_id=statement.id,
                    proposal=proposal.text,
                    rationale=f"{verdict.reason}. {proposal.rationale}",
                    evidence_refs=tuple(
                        EvidenceRef(
                            kind=item.record.source_type,
                            url=item.record.source_url,
                            label=item.record.what[:120],
                            occurred_at=item.record.occurred_at.date(),
                        )
                        for item in (recent or evidence)[:5]
                    ),
                    impact=proposal.impact,
                    batch_id=state.batch_id,
                )
            )
        return state

    def scan_ttls(self, state: ReflectionState) -> ReflectionState:
        state.ttl = ttl_rules.scan(state.statements, state.records, now=state.now)
        return state

    def refresh_passively(self, state: ReflectionState) -> ReflectionState:
        """Silently confirm every statement the evidence still supports.

        This is the node that decides whether the monthly review takes four
        minutes or forty. Everything settled here is something the human is
        never shown.
        """
        for support in state.ttl.refreshed:
            try:
                self.profile.confirm_from_record(support.statement.id, support.record)
                state.refreshed += 1
            except KeyError as exc:
                state.errors.append(str(exc))
        for statement in state.ttl.went_dormant:
            self.store.upsert_statement(statement)
            state.dormant += 1
        return state

    def queue_reconfirmations(self, state: ReflectionState) -> ReflectionState:
        """Queue only the statements with no evidence either way."""
        already = {item.target_id for item in state.queued}
        for statement in state.ttl.needs_review:
            if statement.id in already:
                continue
            elapsed = statement.ttls_elapsed(state.now)
            state.queued.append(
                ReviewItem(
                    kind=ReviewKind.RECONFIRMATION,
                    target_id=statement.id,
                    proposal=statement.statement,
                    rationale=(
                        f"no evidence either way for {elapsed} x {statement.ttl_days} days; "
                        f"confidence has decayed to "
                        f"{statement.effective_confidence(state.now):.2f}"
                    ),
                    impact=round(statement.effective_confidence(state.now), 4),
                    batch_id=state.batch_id,
                )
            )
        return state

    def persist_batch(self, state: ReflectionState) -> ReflectionState:
        """Write the queue. Nothing before this point has touched the review table."""
        for item in state.queued:
            self.store.enqueue_review(item)
        log.info("reflection run complete", extra={"summary": state.summary()})
        return state

    # --- execution ---------------------------------------------------------
    @property
    def nodes(self) -> list[tuple[str, Callable[[ReflectionState], ReflectionState]]]:
        return [
            ("load", self.load),
            ("detect_contradictions", self.detect_contradictions),
            ("open_proposals", self.open_proposals),
            ("scan_ttls", self.scan_ttls),
            ("refresh_passively", self.refresh_passively),
            ("queue_reconfirmations", self.queue_reconfirmations),
            ("persist_batch", self.persist_batch),
        ]

    def build_graph(self) -> Any:
        """Compile the LangGraph state machine, or raise if it is not installed."""
        from langgraph.graph import END, START, StateGraph

        graph: Any = StateGraph(ReflectionState)
        names = [name for name, _ in self.nodes]
        for name, node in self.nodes:
            graph.add_node(name, node)
        graph.add_edge(START, names[0])
        for earlier, later in itertools.pairwise(names):
            graph.add_edge(earlier, later)
        graph.add_edge(names[-1], END)
        return graph.compile()

    def run(
        self,
        *,
        now: datetime | None = None,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        use_langgraph: bool = True,
    ) -> ReflectionState:
        """Execute one pass. Returns the final state either way."""
        state = ReflectionState(now=now or utcnow(), lookback_days=lookback_days)

        if use_langgraph:
            try:
                compiled = self.build_graph()
            except ModuleNotFoundError:
                log.info("langgraph not installed, running the nodes directly")
            else:
                result = compiled.invoke(state)
                return result if isinstance(result, ReflectionState) else _coerce(result, state)

        for name, node in self.nodes:
            try:
                state = node(state)
            except Exception as exc:
                state.errors.append(f"{name}: {exc}")
                log.warning("reflection node failed", extra={"node": name, "error": str(exc)})
                break
        return state


def _coerce(result: Any, fallback: ReflectionState) -> ReflectionState:
    """LangGraph may hand back a mapping; rebuild the dataclass from it."""
    if isinstance(result, dict):
        state = ReflectionState()
        for key, value in result.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state
    return fallback


SEED_EVIDENCE_KIND = SourceType.MANUAL
