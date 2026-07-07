"""The profile repository.

Wraps the store with the lifecycle rules and, more importantly, owns
**selection**: given a project fingerprint and whatever the prompt named, which
of the fifty-odd statements belong in an 800-token slice.

Selection is where the "always injected" promise is either cheap or ruinous. A
profile that dumps every statement on every task is noise the agent learns to
ignore; a profile filtered to what the project actually uses is the thing that
makes the first response correct instead of the third.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from projectmind.config import Settings, get_settings
from projectmind.logging import get_logger
from projectmind.models import (
    Category,
    EpisodicRecord,
    EvidenceRef,
    Fingerprint,
    ProfileStatement,
    ServedStatement,
    StatementStatus,
    utcnow,
)
from projectmind.profile import lifecycle
from projectmind.storage import StatementFilter, Store
from projectmind.tokens import estimate_tokens

log = get_logger(__name__)

#: Relevance multipliers applied on top of a statement's effective confidence.
#: A statement about a tool this project does not use is not wrong, it is just
#: unlikely to matter here, so it is discounted rather than dropped.
RELEVANCE_DIRECT = 1.0
RELEVANCE_GENERAL = 0.9
RELEVANCE_UNRELATED = 0.3

#: Categories that describe the person rather than a stack, and therefore apply
#: regardless of what the project is built with.
STACK_AGNOSTIC = frozenset({Category.WORK_STYLE, Category.CONSTRAINT})


@dataclass(frozen=True, slots=True)
class ScoredStatement:
    statement: ProfileStatement
    score: float
    relevance: float
    reason: str


@dataclass(slots=True)
class ProfileSlice:
    """What selection produced, plus why it stopped."""

    statements: tuple[ScoredStatement, ...] = ()
    tokens: int = 0
    considered: int = 0
    dropped_for_budget: int = 0
    dropped_for_relevance: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.statements

    def served(self) -> tuple[ServedStatement, ...]:
        return tuple(
            ServedStatement(
                id=scored.statement.id,
                statement=scored.statement.statement,
                category=scored.statement.category,
                confidence=round(scored.score, 4),
                scope=scored.statement.scope_label(),
            )
            for scored in self.statements
        )


@dataclass(slots=True)
class DecaySweep:
    """The result of running the decay schedule across the whole profile."""

    examined: int = 0
    decayed: int = 0
    went_dormant: int = 0
    due_for_review: list[UUID] = field(default_factory=list)


@dataclass(slots=True)
class ProfileStats:
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    by_origin: dict[str, int] = field(default_factory=dict)
    mean_confidence: float = 0.0
    oldest_unconfirmed: datetime | None = None


class ProfileRepository:
    """Lifecycle-aware access to profile memory."""

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()

    # --- reads -------------------------------------------------------------
    def get(self, statement_id: UUID) -> ProfileStatement | None:
        return self.store.get_statement(statement_id)

    def active(self) -> list[ProfileStatement]:
        return self.store.list_statements(StatementFilter(statuses=(StatementStatus.ACTIVE,)))

    def all(self) -> list[ProfileStatement]:
        return self.store.list_statements(StatementFilter(statuses=()))

    def by_status(self, *statuses: StatementStatus) -> list[ProfileStatement]:
        return self.store.list_statements(StatementFilter(statuses=statuses))

    # --- writes ------------------------------------------------------------
    def add(self, statement: ProfileStatement) -> ProfileStatement:
        return self.store.upsert_statement(statement)

    def add_many(self, statements: Iterable[ProfileStatement]) -> int:
        count = 0
        for statement in statements:
            self.store.upsert_statement(statement)
            count += 1
        return count

    def activate(self, statement_id: UUID, *, now: datetime | None = None) -> ProfileStatement:
        statement = self._require(statement_id)
        activated = lifecycle.activate(
            statement,
            now=now,
            confidence_floor=self.settings.profile_activation_confidence,
        )
        return self.store.upsert_statement(activated)

    def confirm(
        self,
        statement_id: UUID,
        *,
        evidence: EvidenceRef | None = None,
        now: datetime | None = None,
    ) -> ProfileStatement:
        confirmed = lifecycle.confirm(self._require(statement_id), evidence=evidence, now=now)
        return self.store.upsert_statement(confirmed)

    def confirm_from_record(self, statement_id: UUID, record: EpisodicRecord) -> ProfileStatement:
        confirmed = lifecycle.confirm_from_record(self._require(statement_id), record)
        return self.store.upsert_statement(confirmed)

    def supersede(
        self,
        old_id: UUID,
        replacement: ProfileStatement,
        *,
        now: datetime | None = None,
    ) -> tuple[ProfileStatement, ProfileStatement]:
        retired, new = lifecycle.supersede(self._require(old_id), replacement, now=now)
        self.store.upsert_statement(new)
        self.store.upsert_statement(retired)
        log.info(
            "statement superseded",
            extra={"old": str(retired.id), "new": str(new.id)},
        )
        return retired, new

    def keep_both(
        self,
        first_id: UUID,
        second_id: UUID,
        *,
        first_scope: dict[str, object],
        second_scope: dict[str, object],
        now: datetime | None = None,
    ) -> tuple[ProfileStatement, ProfileStatement]:
        first, second = lifecycle.keep_both(
            self._require(first_id),
            self._require(second_id),
            first_scope=first_scope,
            second_scope=second_scope,
            now=now,
        )
        self.store.upsert_statement(first)
        self.store.upsert_statement(second)
        return first, second

    def reject_contradiction(
        self, statement_id: UUID, *, now: datetime | None = None
    ) -> ProfileStatement:
        cleared = lifecycle.reject_contradiction(self._require(statement_id), now=now)
        return self.store.upsert_statement(cleared)

    def note_contradiction(self, statement_id: UUID, record: EpisodicRecord) -> ProfileStatement:
        noted = lifecycle.note_contradiction(self._require(statement_id), record)
        return self.store.upsert_statement(noted)

    def retire(self, statement_id: UUID, *, now: datetime | None = None) -> ProfileStatement:
        return self.store.upsert_statement(lifecycle.retire(self._require(statement_id), now=now))

    # --- maintenance -------------------------------------------------------
    def sweep_decay(self, *, now: datetime | None = None) -> DecaySweep:
        """Apply the decay schedule to every active statement.

        Only the dormancy transition is persisted. The confidence multipliers
        are computed at read time from `last_confirmed_at`, so there is nothing
        to write for them and no risk of a half-applied sweep leaving the
        profile in a state that disagrees with the clock.
        """
        moment = now or utcnow()
        sweep = DecaySweep()
        for statement in self.active():
            sweep.examined += 1
            outcome = lifecycle.apply_decay(statement, now=moment)
            if outcome.ttls_elapsed >= 1:
                sweep.decayed += 1
            if outcome.went_dormant:
                self.store.upsert_statement(outcome.statement)
                sweep.went_dormant += 1
            if outcome.needs_review:
                sweep.due_for_review.append(statement.id)
        log.info(
            "decay sweep complete",
            extra={
                "examined": sweep.examined,
                "decayed": sweep.decayed,
                "dormant": sweep.went_dormant,
            },
        )
        return sweep

    def due_for_reconfirmation(self, *, now: datetime | None = None) -> list[ProfileStatement]:
        moment = now or utcnow()
        return [s for s in self.active() if s.is_due_for_reconfirmation(moment)]

    def stats(self) -> ProfileStats:
        statements = self.all()
        stats = ProfileStats(total=len(statements))
        if not statements:
            return stats
        for statement in statements:
            stats.by_status[statement.status] = stats.by_status.get(statement.status, 0) + 1
            stats.by_category[statement.category] = stats.by_category.get(statement.category, 0) + 1
            stats.by_origin[statement.origin] = stats.by_origin.get(statement.origin, 0) + 1
        stats.mean_confidence = sum(s.confidence for s in statements) / len(statements)
        stats.oldest_unconfirmed = min(s.last_confirmed_at for s in statements)
        return stats

    # --- selection ---------------------------------------------------------
    def select(
        self,
        *,
        fingerprint: Fingerprint | None = None,
        entities: Sequence[str] = (),
        now: datetime | None = None,
        token_budget: int | None = None,
        max_statements: int | None = None,
        min_relevance: float = RELEVANCE_UNRELATED,
    ) -> ProfileSlice:
        """Pick the statements worth spending the profile budget on.

        Scoring is `effective_confidence x relevance`. Relevance is not a
        similarity score; it is three buckets, because with fifty statements a
        finer-grained ranking would be false precision:

        * **direct** — the statement names something this project uses, or
          something the prompt just mentioned.
        * **general** — the statement names nothing in particular, so it
          applies everywhere. Work style and constraints live here.
        * **unrelated** — the statement is about a stack this project does not
          use. Kept, heavily discounted, and usually squeezed out by the budget.
        """
        moment = now or utcnow()
        budget = token_budget if token_budget is not None else self.settings.profile_token_cap
        cap = max_statements if max_statements is not None else self.settings.max_profile_statements
        prompt_entities = frozenset(entity.lower() for entity in entities)

        scored: list[ScoredStatement] = []
        dropped_for_relevance = 0
        candidates = self.active()

        for statement in candidates:
            confidence = statement.effective_confidence(moment)
            if confidence <= 0.0:
                dropped_for_relevance += 1
                continue
            relevance, reason = self._relevance(statement, fingerprint, prompt_entities)
            if relevance < min_relevance:
                dropped_for_relevance += 1
                continue
            scored.append(
                ScoredStatement(
                    statement=statement,
                    score=confidence * relevance,
                    relevance=relevance,
                    reason=reason,
                )
            )

        scored.sort(key=lambda item: (-item.score, item.statement.statement))

        chosen: list[ScoredStatement] = []
        used = 0
        dropped_for_budget = 0
        for candidate in scored:
            if len(chosen) >= cap:
                dropped_for_budget += 1
                continue
            cost = estimate_tokens(_render_for_budget(candidate.statement))
            if used + cost > budget:
                dropped_for_budget += 1
                continue
            chosen.append(candidate)
            used += cost

        return ProfileSlice(
            statements=tuple(chosen),
            tokens=used,
            considered=len(candidates),
            dropped_for_budget=dropped_for_budget,
            dropped_for_relevance=dropped_for_relevance,
        )

    def _relevance(
        self,
        statement: ProfileStatement,
        fingerprint: Fingerprint | None,
        prompt_entities: frozenset[str],
    ) -> tuple[float, str]:
        statement_entities = frozenset(statement.entities)
        if statement_entities & prompt_entities:
            return RELEVANCE_DIRECT, "named in the prompt"
        if not statement_entities:
            return RELEVANCE_GENERAL, "applies regardless of stack"
        if statement.category in STACK_AGNOSTIC:
            return RELEVANCE_GENERAL, "work style or constraint"
        if fingerprint is not None and fingerprint.shares_any(statement_entities):
            return RELEVANCE_DIRECT, "matches the project fingerprint"
        return RELEVANCE_UNRELATED, "unrelated to this project"

    def _require(self, statement_id: UUID) -> ProfileStatement:
        statement = self.store.get_statement(statement_id)
        if statement is None:
            raise KeyError(f"no profile statement with id {statement_id}")
        return statement


def _render_for_budget(statement: ProfileStatement) -> str:
    """The exact text a statement contributes to a bundle, scope included."""
    scope = statement.scope_label()
    return f"- {statement.statement}" + (f" ({scope})" if scope else "")
