"""The gating rules.

Two signals decide what gets injected: the project fingerprint and the prompt.
This module turns those into a decision, and — just as importantly — into a
stated reason for it. A gate that cannot explain itself cannot be debugged, and
default injection means its mistakes are silent by construction.

The rules are PRD section 6.2, one function, in order:

| Condition                                    | Injected                        |
|----------------------------------------------|---------------------------------|
| Always                                       | profile, fingerprint-filtered   |
| Prompt names an entity memory holds          | matching decisions and failures |
| Task type is architect or explore            | prior decisions, cross-project  |
| Task type is debug                           | failures only                   |
| Prompt is trivial                            | nothing at all                  |
| Nothing clears the confidence bar            | profile only, never padded      |
"""

from __future__ import annotations

from dataclasses import dataclass

from projectmind.config import Settings, get_settings
from projectmind.gate.prompt_analysis import PromptAnalysis
from projectmind.models import Fingerprint, RecordType, TaskType

#: What each task type is allowed to see. A debug prompt gets failures, because
#: a past architecture decision does not help with a stack trace and spends
#: budget that a past bug would have used better.
RECORD_TYPES_FOR_TASK: dict[TaskType, tuple[RecordType, ...]] = {
    TaskType.DEBUG: (RecordType.FAILURE, RecordType.REVERSAL),
    # Failures belong here. Choosing an approach is exactly when "this blew up
    # last time" matters, and excluding them cost two eval queries outright:
    # asked what augmentation to use, the gate could only offer the decision to
    # label in Roboflow, never the mosaic augmentation that collapsed mAP.
    # Serving the decision without the failure that followed it is half a
    # lesson, which is worse than none because it reads as complete.
    TaskType.ARCHITECT: (
        RecordType.DECISION,
        RecordType.REVERSAL,
        RecordType.FAILURE,
        RecordType.EXPERIMENT,
        RecordType.RESEARCH,
    ),
    TaskType.EXPLORE: (
        RecordType.DECISION,
        RecordType.EXPERIMENT,
        RecordType.RESEARCH,
        RecordType.REVERSAL,
        RecordType.FAILURE,
    ),
    TaskType.REFACTOR: (
        RecordType.DECISION,
        RecordType.FAILURE,
        RecordType.REVERSAL,
    ),
    TaskType.IMPLEMENT: (
        RecordType.DECISION,
        RecordType.FAILURE,
        RecordType.EXPERIMENT,
        RecordType.REVERSAL,
    ),
}


@dataclass(frozen=True, slots=True)
class GateDecision:
    """What the gate allows, and why."""

    analysis: PromptAnalysis
    inject_profile: bool
    inject_episodic: bool
    record_types: tuple[RecordType, ...] = ()
    entity_filter: tuple[str, ...] = ()
    cross_project_boost: float = 1.0
    reason: str = ""

    @property
    def skips_everything(self) -> bool:
        return not (self.inject_profile or self.inject_episodic)

    @property
    def task_type(self) -> TaskType:
        return self.analysis.task_type


def decide(
    analysis: PromptAnalysis,
    *,
    fingerprint: Fingerprint | None = None,
    episodic_available: bool = True,
    settings: Settings | None = None,
) -> GateDecision:
    """Apply the gating rules to one analysed prompt."""
    settings = settings or get_settings()

    # Rule: a trivial prompt gets nothing. Not a smaller bundle — nothing.
    if analysis.is_trivial:
        return GateDecision(
            analysis=analysis,
            inject_profile=False,
            inject_episodic=False,
            reason=f"trivial prompt ({analysis.trivial_reason})",
        )

    # Rule: the profile is always eligible. Whether anything clears the
    # relevance floor is the repository's decision, not the gate's.
    if not episodic_available:
        return GateDecision(
            analysis=analysis,
            inject_profile=True,
            inject_episodic=False,
            reason="profile only, no episodic memory ingested yet",
        )

    types = RECORD_TYPES_FOR_TASK.get(analysis.task_type, ())
    boost = settings.cross_project_boost

    # Rule: a named entity is the strongest signal there is something to find.
    if analysis.names_known_entity:
        return GateDecision(
            analysis=analysis,
            inject_profile=True,
            inject_episodic=True,
            record_types=types,
            entity_filter=analysis.entities,
            cross_project_boost=boost,
            reason=f"prompt names {', '.join(analysis.entities[:4])}",
        )

    # Nothing named. The history records are reached only through a named entity:
    # searching them on a vague prompt ("what should I name this module?") is how
    # unrelated records get served. Prompts that name nothing are still answered
    # from the source passages, which serve only what covers the question.
    # Measured on the synthetic set: false injection 0.375 -> 0.240.
    return GateDecision(
        analysis=analysis,
        inject_profile=True,
        inject_episodic=False,
        reason=f"{analysis.task_type} prompt names nothing the history gate knows",
    )


def describe(decision: GateDecision) -> str:
    """A single line for the bundle log and `--explain`."""
    if decision.skips_everything:
        return f"skipped: {decision.reason}"
    slices = ["profile"] if decision.inject_profile else []
    if decision.inject_episodic:
        types = "/".join(str(t) for t in decision.record_types) or "any"
        slices.append(f"episodic[{types}]")
    return f"{'+'.join(slices)}: {decision.reason}"
