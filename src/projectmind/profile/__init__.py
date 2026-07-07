"""Profile memory: the small, high-confidence layer that is always injected."""

from projectmind.profile.lifecycle import (
    ContradictionVerdict,
    DecayOutcome,
    TransitionError,
    activate,
    apply_decay,
    confirm,
    confirm_from_record,
    evaluate_contradictions,
    keep_both,
    note_contradiction,
    reject_contradiction,
    retire,
    supersede,
)
from projectmind.profile.repository import (
    DecaySweep,
    ProfileRepository,
    ProfileSlice,
    ProfileStats,
    ScoredStatement,
)

__all__ = [
    "ContradictionVerdict",
    "DecayOutcome",
    "DecaySweep",
    "ProfileRepository",
    "ProfileSlice",
    "ProfileStats",
    "ScoredStatement",
    "TransitionError",
    "activate",
    "apply_decay",
    "confirm",
    "confirm_from_record",
    "evaluate_contradictions",
    "keep_both",
    "note_contradiction",
    "reject_contradiction",
    "retire",
    "supersede",
]
