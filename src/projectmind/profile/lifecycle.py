"""How a preference lives and dies.

Two mechanisms, both required, neither sufficient alone. Supersession catches
sharp reversals; re-confirmation catches slow drift. See PRD section 5.

Everything in this module is a pure function over statements. Nothing here
touches a database, opens a review or asks a question — those are the
repository's and the reflection loop's jobs. Keeping the rules pure is what
makes the decay thresholds testable without a clock or a store.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from projectmind.models import (
    DECAY_FACTORS,
    EpisodicRecord,
    EvidenceRef,
    Origin,
    ProfileStatement,
    RecordType,
    SourceType,
    StatementStatus,
    utcnow,
)


class TransitionError(ValueError):
    """Raised when a lifecycle transition would violate an invariant."""


# --------------------------------------------------------------------------- #
# Activation
# --------------------------------------------------------------------------- #


def activate(
    statement: ProfileStatement,
    *,
    now: datetime | None = None,
    confidence_floor: float = 0.85,
) -> ProfileStatement:
    """Move a proposed statement to active.

    The confidence bar is high on purpose. A wrong profile statement poisons
    every future task, so the cost of a false positive here is not comparable
    to the cost of a false positive in episodic memory.
    """
    if statement.status is StatementStatus.SUPERSEDED:
        raise TransitionError("a superseded statement cannot be reactivated")
    if statement.confidence < confidence_floor:
        raise TransitionError(
            f"confidence {statement.confidence:.2f} is below the activation bar "
            f"of {confidence_floor:.2f}"
        )
    moment = now or utcnow()
    return statement.model_copy(
        update={"status": StatementStatus.ACTIVE, "last_confirmed_at": moment}
    )


# --------------------------------------------------------------------------- #
# Supersession — the three outcomes of a proposal
# --------------------------------------------------------------------------- #


def supersede(
    old: ProfileStatement,
    new: ProfileStatement,
    *,
    now: datetime | None = None,
) -> tuple[ProfileStatement, ProfileStatement]:
    """Retire `old` in favour of `new`, keeping the link in both directions.

    Nothing is deleted. The timeline of how someone changed their mind is
    itself worth having, and a superseded statement is the only way to answer
    "when did I stop doing that".
    """
    if old.id == new.id:
        raise TransitionError("a statement cannot supersede itself")
    if old.status is StatementStatus.SUPERSEDED:
        raise TransitionError(f"{old.id} is already superseded")
    moment = now or utcnow()
    retired = old.model_copy(update={"status": StatementStatus.SUPERSEDED, "superseded_by": new.id})
    replacement = new.model_copy(
        update={
            "status": StatementStatus.ACTIVE,
            "supersedes": old.id,
            "last_confirmed_at": moment,
            "contradiction_count": 0,
            "contradiction_refs": (),
            "origin": new.origin or Origin.REFLECTION,
        }
    )
    return retired, replacement


def keep_both(
    first: ProfileStatement,
    second: ProfileStatement,
    *,
    first_scope: dict[str, object],
    second_scope: dict[str, object],
    now: datetime | None = None,
) -> tuple[ProfileStatement, ProfileStatement]:
    """Resolve a proposal as "these are context-dependent, not contradictory".

    Both stay active and each gains a scope qualifier, which is what lets the
    gate tell them apart later. Refusing to add scopes would leave two
    statements the gate has no way to choose between, so they are required.
    """
    if not first_scope or not second_scope:
        raise TransitionError("keeping both requires a scope qualifier on each statement")
    moment = now or utcnow()
    common = {
        "status": StatementStatus.ACTIVE,
        "last_confirmed_at": moment,
        "contradiction_count": 0,
        "contradiction_refs": (),
    }
    return (
        first.model_copy(update={**common, "scope": dict(first_scope)}),
        second.model_copy(update={**common, "scope": dict(second_scope)}),
    )


def reject_contradiction(
    statement: ProfileStatement, *, now: datetime | None = None
) -> ProfileStatement:
    """Resolve a proposal as "the contradicting evidence was noise".

    The statement's clock is reset and its contradiction count cleared. The
    rejection itself is logged by the caller, because repeated false
    contradictions against the same statement mean the extractor is
    systematically misreading a class of artifact — that is extractor signal,
    not a fact about the preference.
    """
    moment = now or utcnow()
    return statement.model_copy(
        update={
            "last_confirmed_at": moment,
            "contradiction_count": 0,
            "contradiction_refs": (),
        }
    )


# --------------------------------------------------------------------------- #
# Contradiction accounting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ContradictionVerdict:
    """Whether the evidence against a statement is enough to ask a human."""

    should_propose: bool
    reason: str
    supporting: tuple[UUID, ...] = ()

    def __bool__(self) -> bool:
        return self.should_propose


def note_contradiction(
    statement: ProfileStatement,
    record: EpisodicRecord,
) -> ProfileStatement:
    """Record that one episodic record contradicts this statement."""
    ref = str(record.id)
    if ref in statement.contradiction_refs:
        return statement
    return statement.model_copy(
        update={
            "contradiction_count": statement.contradiction_count + 1,
            "contradiction_refs": (*statement.contradiction_refs, ref),
        }
    )


def evaluate_contradictions(
    statement: ProfileStatement,
    contradicting: Sequence[EpisodicRecord],
    *,
    now: datetime | None = None,
    window_days: int = 90,
    threshold: int = 3,
) -> ContradictionVerdict:
    """Decide whether contradicting evidence warrants a supersession proposal.

    One contradicting record is not enough; a single experimental branch should
    not overturn a settled preference. Three inside the window is, and so is a
    single record the extractor explicitly typed as a reversal, because a
    reversal is an author stating the change rather than a reader inferring it.
    """
    if statement.status is not StatementStatus.ACTIVE:
        return ContradictionVerdict(False, "statement is not active")

    moment = now or utcnow()
    cutoff = moment - timedelta(days=window_days)
    recent = [
        record
        for record in contradicting
        if _aware(record.occurred_at) >= cutoff and _aware(record.occurred_at) <= moment
    ]

    reversals = [record for record in recent if record.type is RecordType.REVERSAL]
    if reversals:
        return ContradictionVerdict(
            True,
            "an explicit reversal was recorded",
            tuple(record.id for record in reversals),
        )

    if len(recent) >= threshold:
        return ContradictionVerdict(
            True,
            f"{len(recent)} contradicting records within {window_days} days",
            tuple(record.id for record in recent),
        )

    return ContradictionVerdict(
        False,
        f"only {len(recent)} of {threshold} contradicting records within {window_days} days",
        tuple(record.id for record in recent),
    )


def _aware(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Re-confirmation and decay
# --------------------------------------------------------------------------- #


def confirm(
    statement: ProfileStatement,
    *,
    evidence: EvidenceRef | None = None,
    now: datetime | None = None,
) -> ProfileStatement:
    """Refresh the clock, optionally attaching the evidence that did it.

    Re-confirmation is passive first: when supporting evidence arrives during
    the TTL window this runs silently and the human is never asked. They are
    only asked about statements with no evidence either way.
    """
    moment = now or utcnow()
    refs = statement.evidence_refs
    if evidence is not None and evidence not in refs:
        refs = (*refs, evidence)
    updates: dict[str, object] = {"last_confirmed_at": moment, "evidence_refs": refs}
    if statement.status is StatementStatus.DORMANT:
        updates["status"] = StatementStatus.ACTIVE
    return statement.model_copy(update=updates)


def confirm_from_record(
    statement: ProfileStatement,
    record: EpisodicRecord,
    *,
    now: datetime | None = None,
) -> ProfileStatement:
    evidence = EvidenceRef(
        kind=record.source_type,
        url=record.source_url,
        label=record.what[:120],
        occurred_at=record.occurred_at.date(),
    )
    return confirm(statement, evidence=evidence, now=now or record.occurred_at)


def retire(statement: ProfileStatement, *, now: datetime | None = None) -> ProfileStatement:
    """Mark a statement dormant. It stops being served and joins the next review."""
    del now
    return statement.model_copy(update={"status": StatementStatus.DORMANT})


@dataclass(frozen=True, slots=True)
class DecayOutcome:
    """What a decay sweep decided about one statement."""

    statement: ProfileStatement
    ttls_elapsed: int
    effective_confidence: float
    went_dormant: bool
    needs_review: bool

    @property
    def changed(self) -> bool:
        return self.went_dormant


def apply_decay(statement: ProfileStatement, *, now: datetime | None = None) -> DecayOutcome:
    """Evaluate one statement against the decay schedule.

    The schedule degrades before it deletes:

        1 TTL over  -> confidence x0.8, still served
        2 TTLs over -> confidence x0.5, served only if nothing better ranks
        3 TTLs over -> dormant, not served, surfaced at the next review
    """
    moment = now or utcnow()
    elapsed = statement.ttls_elapsed(moment)
    if statement.status is not StatementStatus.ACTIVE:
        return DecayOutcome(
            statement, elapsed, statement.effective_confidence(moment), False, False
        )

    if elapsed >= len(DECAY_FACTORS):
        return DecayOutcome(retire(statement, now=moment), elapsed, 0.0, True, True)

    return DecayOutcome(
        statement,
        elapsed,
        statement.effective_confidence(moment),
        False,
        needs_review=elapsed >= 1,
    )


SEED_EVIDENCE = EvidenceRef(
    kind=SourceType.MANUAL,
    url="manual://seed",
    label="hand-written at seed time",
)
