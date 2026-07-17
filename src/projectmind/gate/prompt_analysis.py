"""Reading the prompt.

Three questions, answered without a model call because this runs on every
prompt: what kind of task is this, what does it name, and is any of it new
territory for this project.

The hardest of the three is **triviality**, and it is the one that matters
most. A rename does not benefit from memory, and injecting into one costs
tokens, costs attention, and trains the reader to ignore the block. The PRD
budgets nothing for trivial prompts, so the classifier has to recognise them
without also swallowing real work that happens to be phrased briefly.

The rule used here is structural rather than a list of remembered examples:
a prompt is trivial when its main verb describes a **mechanical, locally-scoped
edit**, or when it asks for an explanation of something already on screen.
Strongly mechanical verbs (rename, reformat, sort imports) are trivial on their
own. Weaker ones (convert, inline, move) are trivial only when the prompt is
short and names nothing memory knows about.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from projectmind.config import Settings, get_settings
from projectmind.entities import extract
from projectmind.models import Fingerprint, TaskType

# --------------------------------------------------------------------------- #
# Triviality
# --------------------------------------------------------------------------- #

#: Edits whose correct form does not depend on anything the developer has done
#: before. Trivial regardless of length or what they mention.
STRICTLY_MECHANICAL = (
    r"\brename\b",
    r"\bre-?format\b",
    r"\bformat (?:this|the|it)\b",
    r"\bsort (?:the |these |those |my )?imports?\b",
    r"\b(?:fix|correct) (?:a |the )?typos?\b",
    r"\badd (?:a )?docstrings?\b",
    r"\badd (?:a )?(?:trailing )?newline\b",
    r"\b(?:delete|remove) (?:the |this |an? )?unused\b",
    r"\bbump (?:the )?version\b",
    r"\b(?:re-?)?indent\b",
    r"\bwrap (?:this|the) lines?\b",
    r"\badd (?:a )?type hints? to\b",
    r"\balphabet(?:ise|ize)\b",
)

#: Edits that are usually mechanical but can be the tip of real work. Trivial
#: only when the prompt is short and mentions nothing memory holds.
WEAKLY_MECHANICAL = (
    r"\bconvert (?:this|the|it)\b",
    r"\binline (?:this|the)\b",
    r"\bextract (?:this|the) (?:variable|constant|method)\b",
    r"\bmove (?:this|the) (?:function|class|method)\b",
    r"\brewrite (?:this|the) (?:line|expression)\b",
    r"\bswap\b",
)

#: Questions about code already in front of the reader.
EXPLAIN_PATTERNS = (
    r"^what (?:does|is) (?:this|that)\b",
    r"^explain (?:this|the|what)\b",
    r"^what'?s (?:this|that)\b",
    r"\bwhat does (?:this|the) \w+ do\b",
)

# --------------------------------------------------------------------------- #
# Task type
# --------------------------------------------------------------------------- #

TASK_SIGNALS: dict[TaskType, tuple[str, ...]] = {
    TaskType.DEBUG: (
        r"\berrors?\b",
        r"\bbugs?\b",
        r"\bcrash(?:es|ing|ed)?\b",
        r"\bfail(?:s|ing|ed|ure)?\b",
        r"\bexceptions?\b",
        r"\btraceback\b",
        r"\bstack trace\b",
        r"\bnot working\b",
        r"\bdoes ?n'?o?t work\b",
        r"\bbroken\b",
        r"\bwhy (?:is|does|am|are|do)\b",
        r"\bnan\b",
        r"\btimed? ?out\b",
        r"\bout of memory\b",
        r"\boom\b",
        r"\bhangs?\b",
        r"\bstalls?\b",
        r"\bdrifts?\b",
        r"\bwrong (?:answer|output|result)\b",
        r"\bworse\b",
        r"\bterrible\b",
        r"\bcollapse[ds]?\b",
        r"\bmismatch\b",
        r"\bleak(?:s|ing|age)?\b",
        r"\bcannot parse\b",
        r"\bmalformed\b",
        r"\bdegrad(?:es|ed|ing)\b",
        r"\bslow(?:er)?\b",
        r"\bidle\b",
        r"\butili[sz]ation\b",
        r"\bbottleneck\b",
        r"\b(?:is|are|seems?|looks?|sits?) (?:at |too )?low\b",
        r"\bimplausib\w+\b",
        r"\bsuspicious\w*\b",
        r"\bunexpected\w*\b",
        r"\bregress(?:ion|ed|ing)?\b",
    ),
    TaskType.ARCHITECT: (
        r"\bshould i\b",
        r"\bwhich (?:one|approach|library|framework|database|tool)\b",
        r"\bdesign(?:ing)?\b",
        r"\barchitect(?:ure)?\b",
        r"\bstructure\b",
        r"\bhow should i\b",
        r"\bwhat'?s the best way\b",
        # An alternation inside a question is a choice being posed, whether or
        # not the "?" lands immediately after it: "Firebase or Supabase for the
        # backend?" is the same question as "Firebase or Supabase?".
        r"\b[\w.-]+ or [\w.-]+\b(?=[^?]*\?)",
        r"\bstarting a new\b",
        r"\bfrom scratch\b",
        r"\btrade-?offs?\b",
        r"\bwhere should\b",
        r"\boptions\b",
    ),
    TaskType.EXPLORE: (
        # Questions explicitly about the developer's own past. These are the
        # canonical case for a cross-project memory layer and were landing in
        # `implement` by default, which serves nothing without a named entity:
        # "How did I set up the eval harness before?" returned an empty bundle
        # against a corpus that contained the answer.
        r"\bhow did i\b",
        r"\bwhat did i\b",
        r"\bdid i (?:ever|use|try|do|end up)\b",
        r"\bhave i (?:ever|done|used|tried)\b",
        r"\blast time\b",
        r"\bpreviously\b",
        r"\bin the past\b",
        r"\bwhat if\b",
        r"\bis it worth\b",
        r"\bany thoughts\b",
        r"\bthinking about\b",
        r"\bresearch(?:ing)?\b",
        r"\bcompare\b",
        r"\binvestigate\b",
        r"\bbefore i\b",
        r"\bfastest route\b",
        r"\bhave i\b",
        r"\bdid i ever\b",
        r"\bexplore\b",
    ),
    TaskType.REFACTOR: (
        r"\brefactor\b",
        r"\bclean ?up\b",
        r"\brestructure\b",
        r"\bsimplify\b",
        r"\bsplit (?:this|the|up)\b",
        r"\bdeduplicat\w*\b",
        r"\btidy\b",
        r"\bmake (?:this|it) reproducible\b",
    ),
    TaskType.IMPLEMENT: (
        r"\badd\b",
        r"\bimplement\b",
        r"\bbuild\b",
        r"\bcreate\b",
        r"\bwrite\b",
        r"\bset ?up\b",
        r"\bwire (?:up|in)\b",
        r"\bsupport for\b",
        r"\bhandle\b",
        r"\bhook up\b",
    ),
}

#: Debug beats architect beats explore beats refactor beats implement when
#: scores tie. A prompt that sounds like both a bug and a design question is
#: nearly always a bug being described.
TASK_PRIORITY: tuple[TaskType, ...] = (
    TaskType.DEBUG,
    TaskType.ARCHITECT,
    TaskType.EXPLORE,
    TaskType.REFACTOR,
    TaskType.IMPLEMENT,
)


@dataclass(frozen=True, slots=True)
class PromptAnalysis:
    """What the gate needs to know about a prompt."""

    prompt: str
    task_type: TaskType
    entities: tuple[str, ...] = ()
    novelty: float = 0.0
    word_count: int = 0
    signals: dict[str, int] = field(default_factory=dict)
    trivial_reason: str = ""

    @property
    def is_trivial(self) -> bool:
        return self.task_type is TaskType.TRIVIAL

    @property
    def names_known_entity(self) -> bool:
        return bool(self.entities)

    @property
    def is_novel_territory(self) -> bool:
        """The prompt is mostly about things this project does not already use."""
        return self.novelty >= 0.5


def _matches(patterns: Iterable[str], text: str) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text)]


def _signal_strength(patterns: Iterable[str], text: str) -> int:
    """How many distinct places in the prompt point at this task type.

    Counting matched patterns would over-count: `should i` and `how should i`
    both fire on the same five words and would score a prompt as twice as
    architectural as it is. Overlapping spans are merged so each region of the
    prompt votes once.
    """
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        spans.extend(match.span() for match in re.finditer(pattern, text))
    if not spans:
        return 0
    spans.sort()
    merged = 1
    current_end = spans[0][1]
    for start, end in spans[1:]:
        if start >= current_end:
            merged += 1
            current_end = end
        else:
            current_end = max(current_end, end)
    return merged


def classify(
    prompt: str,
    *,
    vocabulary: Sequence[str] = (),
    fingerprint: Fingerprint | None = None,
    settings: Settings | None = None,
) -> PromptAnalysis:
    """Classify a prompt. Deterministic, no network, microseconds."""
    settings = settings or get_settings()
    text = prompt.strip().lower()
    words = text.split()
    entities = extract(prompt, vocabulary) if vocabulary else ()

    novelty = 0.0
    if entities and fingerprint is not None:
        known = (
            frozenset(fingerprint.languages)
            | frozenset(fingerprint.frameworks)
            | frozenset(fingerprint.dependencies)
        )
        unknown = [entity for entity in entities if entity not in known]
        novelty = len(unknown) / len(entities)

    trivial_reason = _triviality(text, words, entities, settings)
    if trivial_reason:
        return PromptAnalysis(
            prompt=prompt,
            task_type=TaskType.TRIVIAL,
            entities=entities,
            novelty=novelty,
            word_count=len(words),
            trivial_reason=trivial_reason,
        )

    scores = {task: _signal_strength(patterns, text) for task, patterns in TASK_SIGNALS.items()}
    best = max(
        TASK_PRIORITY,
        key=lambda task: (scores.get(task, 0), -TASK_PRIORITY.index(task)),
    )
    if scores.get(best, 0) == 0:
        best = TaskType.IMPLEMENT

    return PromptAnalysis(
        prompt=prompt,
        task_type=best,
        entities=entities,
        novelty=novelty,
        word_count=len(words),
        signals={str(task): count for task, count in scores.items() if count},
    )


def _triviality(
    text: str,
    words: list[str],
    entities: tuple[str, ...],
    settings: Settings,
) -> str:
    """Why this prompt is trivial, or an empty string when it is not."""
    if not text:
        return "empty prompt"

    if _matches(STRICTLY_MECHANICAL, text):
        return "mechanical edit"

    if _matches(EXPLAIN_PATTERNS, text) and len(words) <= settings.trivial_prompt_max_words:
        return "asks about code already on screen"

    if (
        _matches(WEAKLY_MECHANICAL, text)
        and len(words) <= settings.trivial_prompt_max_words
        and not entities
    ):
        return "short local edit naming nothing memory holds"

    return ""
