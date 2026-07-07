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

__all__ = [
    "ContradictionVerdict",
    "DecayOutcome",
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
