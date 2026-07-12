"""Token budgets, enforced server-side.

Hard caps, from PRD section 6.3: 800 tokens of profile, 1500 of episodic, 2300
in total. Enforced here rather than trusted to the caller, because the whole
point of a single front door is that the answer does not vary by which agent
asked.

Two properties matter more than the exact numbers:

* **Never pad.** Filling the budget is not a goal. If three records clear the
  bar and the cap allows five, three are served.
* **Never overflow.** A record that does not fit is dropped whole rather than
  truncated. Half a decision with its reasoning cut off is worse than no
  decision, because the reader cannot tell it is half.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from projectmind.config import Settings, get_settings
from projectmind.models import ServedRecord, ServedStatement
from projectmind.tokens import estimate_tokens


@dataclass(frozen=True, slots=True)
class BudgetReport:
    """What survived the budget, and what did not."""

    kept: tuple[ServedRecord, ...]
    tokens: int
    dropped_for_cap: int = 0
    dropped_for_budget: int = 0

    @property
    def dropped(self) -> int:
        return self.dropped_for_cap + self.dropped_for_budget


def record_cost(record: ServedRecord) -> int:
    """Exactly what this record will contribute once rendered."""
    return estimate_tokens(record.render())


def statement_cost(statement: ServedStatement) -> int:
    return estimate_tokens(statement.render())


def fit_records(
    records: Sequence[ServedRecord],
    *,
    token_cap: int,
    max_records: int,
) -> BudgetReport:
    """Take records in order until a cap is reached.

    Order is the ranker's output and is never second-guessed here. A record
    that does not fit is skipped rather than ending the loop, so a single long
    record cannot starve the shorter ones behind it.
    """
    kept: list[ServedRecord] = []
    used = 0
    dropped_for_cap = 0
    dropped_for_budget = 0

    for record in records:
        if len(kept) >= max_records:
            dropped_for_cap += 1
            continue
        cost = record_cost(record)
        if used + cost > token_cap:
            dropped_for_budget += 1
            continue
        kept.append(record)
        used += cost

    return BudgetReport(tuple(kept), used, dropped_for_cap, dropped_for_budget)


def enforce_total(
    profile_tokens: int,
    episodic: BudgetReport,
    *,
    settings: Settings | None = None,
) -> BudgetReport:
    """Trim the episodic slice until the combined total fits.

    The profile is never trimmed at this stage. It is the layer that applies to
    every task, it is already capped at 800, and dropping a preference to make
    room for one more war story is the wrong trade.
    """
    settings = settings or get_settings()
    total_cap = settings.total_token_cap
    if profile_tokens + episodic.tokens <= total_cap:
        return episodic

    allowance = max(0, total_cap - profile_tokens)
    trimmed: list[ServedRecord] = []
    used = 0
    for record in episodic.kept:
        cost = record_cost(record)
        if used + cost > allowance:
            break
        trimmed.append(record)
        used += cost

    return BudgetReport(
        tuple(trimmed),
        used,
        episodic.dropped_for_cap,
        episodic.dropped_for_budget + (len(episodic.kept) - len(trimmed)),
    )


def within_caps(
    profile_tokens: int, episodic_tokens: int, settings: Settings | None = None
) -> bool:
    """Assertion helper: every cap holds. Used by tests and by `doctor`."""
    settings = settings or get_settings()
    return (
        profile_tokens <= settings.profile_token_cap
        and episodic_tokens <= settings.episodic_token_cap
        and profile_tokens + episodic_tokens <= settings.total_token_cap
    )
