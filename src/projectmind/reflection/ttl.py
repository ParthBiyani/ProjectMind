"""The other half of preference death: the ones that die quietly.

Supersession catches sharp reversals. This catches drift — you did not decide
to stop using it, you just stopped, and nothing in the record says so.

**Passive first, active second.** If supporting evidence arrived during the TTL
window, the clock is refreshed silently and the human is never asked. They are
only asked about statements with no evidence either way, which is the small
remainder that a human genuinely has to adjudicate. Getting this ordering
backwards is how a review that should take four minutes takes forty and then
never happens again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from projectmind.logging import get_logger
from projectmind.models import EpisodicRecord, ProfileStatement, StatementStatus, utcnow
from projectmind.profile import lifecycle

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Support:
    """Evidence that a statement is still true."""

    statement: ProfileStatement
    record: EpisodicRecord
    entity: str


@dataclass(slots=True)
class TtlScan:
    """What one pass over the profile found."""

    examined: int = 0
    refreshed: list[Support] = field(default_factory=list)
    needs_review: list[ProfileStatement] = field(default_factory=list)
    went_dormant: list[ProfileStatement] = field(default_factory=list)

    @property
    def asked_nothing_for(self) -> int:
        """How many statements were settled without troubling anyone."""
        return len(self.refreshed)

    def summary(self) -> str:
        return (
            f"{self.examined} examined, {len(self.refreshed)} refreshed silently, "
            f"{len(self.needs_review)} need a human, {len(self.went_dormant)} went dormant"
        )


def find_support(
    statement: ProfileStatement,
    records: Sequence[EpisodicRecord],
    *,
    since: datetime,
) -> Support | None:
    """The most recent record that agrees with the statement, if any.

    Agreement is the absence of disagreement plus an entity in common: a record
    that names what the statement is about, arrived inside the window, and did
    not reject it. That is a weaker bar than contradiction detection uses, and
    deliberately so — the cost of silently refreshing a statement that is still
    true is nothing, while the cost of asking a human about it is their
    patience.
    """
    from projectmind.reflection.contradiction import check

    entities = frozenset(statement.entities)
    if not entities:
        return None

    supporting = [
        record
        for record in records
        if record.occurred_at >= since
        and entities & frozenset(record.entities)
        and check(statement, record) is None
    ]
    if not supporting:
        return None

    latest = max(supporting, key=lambda record: record.occurred_at)
    return Support(
        statement=statement,
        record=latest,
        entity=sorted(entities & frozenset(latest.entities))[0],
    )


def scan(
    statements: Sequence[ProfileStatement],
    records: Sequence[EpisodicRecord],
    *,
    now: datetime | None = None,
) -> TtlScan:
    """Walk the profile, refreshing what evidence supports and queuing the rest."""
    moment = now or utcnow()
    result = TtlScan()

    for statement in statements:
        if statement.status is not StatementStatus.ACTIVE:
            continue
        result.examined += 1

        outcome = lifecycle.apply_decay(statement, now=moment)
        if not outcome.needs_review:
            continue

        window_start = statement.last_confirmed_at
        support = find_support(statement, records, since=window_start)
        if support is not None:
            result.refreshed.append(support)
            continue

        if outcome.went_dormant:
            result.went_dormant.append(outcome.statement)
        else:
            result.needs_review.append(statement)

    log.info("ttl scan complete", extra={"summary": result.summary()})
    return result
