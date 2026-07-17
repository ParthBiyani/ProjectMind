"""Detecting when the evidence stopped agreeing with the profile.

Checking every new episodic record against every active profile statement is
O(n x m). With 200 statements and a few thousand records that is hundreds of
thousands of comparisons, and if each one were a model call it would be
unaffordable. So the expensive step never runs first:

    entity overlap  ->  structural rules  ->  (optionally) a model

The entity prefilter throws away the overwhelming majority of pairs for free —
a statement about Riverpod cannot be contradicted by a record about Postgres
backups, and no amount of language understanding changes that. What survives is
checked by structural rules that are cheap and explainable. A model is only
consulted for pairs the rules find ambiguous, and only when one is configured.

A contradiction found here is never acted on automatically. It raises a
proposal for a human, which is the whole point of the design.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from projectmind.entities import normalise
from projectmind.logging import get_logger
from projectmind.models import Category, EpisodicRecord, ProfileStatement, RecordType

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Contradiction:
    """One record disagreeing with one statement, and why we think so."""

    statement: ProfileStatement
    record: EpisodicRecord
    rule: str
    entity: str
    strength: float

    def describe(self) -> str:
        return f"{self.record.what[:80]} ({self.rule} on {self.entity})"


@dataclass(slots=True)
class DetectionStats:
    """What the prefilter saved, which is the point of having one."""

    pairs_possible: int = 0
    pairs_after_prefilter: int = 0
    contradictions: int = 0
    model_calls: int = 0

    @property
    def prefilter_saving(self) -> float:
        if not self.pairs_possible:
            return 0.0
        return 1.0 - (self.pairs_after_prefilter / self.pairs_possible)


#: Phrases after which an abandonment statement stops talking about the thing
#: it abandoned and starts talking about the replacement.
_PIVOTS = (" in favour of ", " in favor of ", " instead of ", "; ", " rather than ")


def subject_entities(statement: ProfileStatement) -> frozenset[str]:
    """The entities a statement is actually *about*.

    A statement's `entities` list is a retrieval aid: it holds what the
    statement prefers and what it contrasts against, so that naming either one
    in a prompt surfaces it. For contradiction detection that conflation is
    fatal. "Uses Supabase for backend-as-a-service" carries `firebase` as a
    contrast entity, so a record reading "adopted Supabase instead of Firebase"
    matched the `rejected` rule and was reported as *contradicting* the very
    preference it confirms. Measured on the fixture corpus, that one confusion
    produced six of sixteen proposals, all backwards.

    The subject is therefore narrowed to entities the statement text actually
    names, and for an abandonment, to those named before it pivots to the
    replacement: "Abandoned k-fold in favour of walk-forward" is about k-fold.
    """
    text = statement.statement.lower()
    if statement.category is Category.ABANDONED:
        # "Abandoned Provider for Flutter state management" is about Provider.
        # Flutter is where it happened. Narrowing to the words between
        # "abandoned" and the first scope preposition stops the platform, the
        # domain and the replacement from all counting as the abandoned thing —
        # which had "copied the Riverpod skeleton" contradicting "abandoned
        # Provider", on the strength of them both mentioning Flutter.
        start = text.find("abandoned")
        if start >= 0:
            text = text[start + len("abandoned") :]
        for pivot in (*_PIVOTS, " for ", " after ", " when ", " where ", " on ", " because "):
            index = text.find(pivot)
            if index > 0:
                text = text[:index]
                break

    named = {
        entity
        for entity in statement.entities
        if entity in text or entity.replace("-", " ") in text or entity.replace("-", "") in text
    }
    # A statement that names nothing recognisable falls back to its full list;
    # better a noisy check than no check at all.
    return frozenset(named or statement.entities)


def _named_in_headline(entities: frozenset[str], record: EpisodicRecord) -> str | None:
    """The first of `entities` the record's own headline actually mentions.

    An entity list includes everything a record touches, including the platform
    it happened on. Requiring the word to appear in `what` is the difference
    between "this reversal was about Firestore" and "this reversal happened in
    a Flutter app, and you have a statement about Flutter".
    """
    headline = f"{record.what} {record.outcome}".lower()
    for entity in sorted(entities):
        if entity in headline or entity.replace("-", " ") in headline:
            return entity
    return None


def _rejected(record: EpisodicRecord) -> frozenset[str]:
    """Alternatives the record explicitly turned down, normalised to entities."""
    tokens: set[str] = set()
    for alternative in record.rejected_alternatives:
        for part in alternative.replace("/", " ").replace(",", " ").split():
            cleaned = normalise(part)
            if len(cleaned) > 2:
                tokens.add(cleaned)
    return frozenset(tokens)


def check(statement: ProfileStatement, record: EpisodicRecord) -> Contradiction | None:
    """Structural contradiction rules. Cheap, deterministic, explainable.

    Three rules, each anchored on a shared entity so the reasoning can always
    be pointed at a specific word:

    * **rejected** — the record explicitly turned down the thing the statement
      says is preferred. This is the strongest available signal short of the
      author writing "I was wrong".
    * **reversal** — the record is typed as a reversal and names the thing.
    * **readopted** — the statement says something was abandoned and a later
      decision adopts it anyway.
    """
    statement_entities = subject_entities(statement)
    if not statement_entities:
        return None

    record_entities = frozenset(record.entities)
    if not (statement_entities & (record_entities | _rejected(record))):
        return None

    if statement.category in {Category.TOOL_PREFERENCE, Category.WORK_STYLE}:
        turned_down = statement_entities & _rejected(record)
        if turned_down:
            return Contradiction(
                statement=statement,
                record=record,
                rule="rejected",
                entity=sorted(turned_down)[0],
                strength=0.9,
            )
        if record.type is RecordType.REVERSAL:
            about = _named_in_headline(statement_entities & record_entities, record)
            if about:
                return Contradiction(
                    statement=statement,
                    record=record,
                    rule="reversal",
                    entity=about,
                    strength=0.85,
                )

    if statement.category is Category.ABANDONED and record.type is RecordType.DECISION:
        about = _named_in_headline(statement_entities & record_entities - _rejected(record), record)
        if about:
            return Contradiction(
                statement=statement,
                record=record,
                rule="readopted",
                entity=about,
                strength=0.8,
            )

    return None


def detect(
    statements: Sequence[ProfileStatement],
    records: Sequence[EpisodicRecord],
) -> tuple[dict[str, list[Contradiction]], DetectionStats]:
    """Find every contradiction between the two sets, grouped by statement id.

    The entity index is built once over the records so the prefilter is a set
    lookup rather than a scan per statement.
    """
    stats = DetectionStats(pairs_possible=len(statements) * len(records))

    by_entity: dict[str, list[EpisodicRecord]] = {}
    for record in records:
        for entity in frozenset(record.entities) | _rejected(record):
            by_entity.setdefault(entity, []).append(record)

    found: dict[str, list[Contradiction]] = {}
    for statement in statements:
        candidates: dict[str, EpisodicRecord] = {}
        for entity in subject_entities(statement):
            for record in by_entity.get(entity, ()):
                candidates[str(record.id)] = record
        stats.pairs_after_prefilter += len(candidates)

        for record in candidates.values():
            contradiction = check(statement, record)
            if contradiction is not None:
                found.setdefault(str(statement.id), []).append(contradiction)
                stats.contradictions += 1

    log.info(
        "contradiction scan complete",
        extra={
            "statements": len(statements),
            "records": len(records),
            "pairs_possible": stats.pairs_possible,
            "pairs_checked": stats.pairs_after_prefilter,
            "saving": round(stats.prefilter_saving, 4),
            "found": stats.contradictions,
        },
    )
    return found, stats


# --------------------------------------------------------------------------- #
# Turning a contradiction into a proposal
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Proposal:
    """A suggested replacement statement, for a human to accept or reject."""

    statement: ProfileStatement
    text: str
    rationale: str
    evidence: list[Contradiction] = field(default_factory=list)
    impact: float = 0.0

    @property
    def winners(self) -> tuple[str, ...]:
        """What the evidence suggests is being used instead."""
        counts: dict[str, int] = {}
        losing = frozenset(self.statement.entities)  # the whole list, not just the subject
        for contradiction in self.evidence:
            for entity in contradiction.record.entities:
                if entity not in losing:
                    counts[entity] = counts.get(entity, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return tuple(name for name, _ in ranked[:3])


def propose(
    statement: ProfileStatement,
    evidence: Sequence[Contradiction],
    *,
    phrasing: str | None = None,
) -> Proposal:
    """Draft a replacement.

    The wording is deliberately mechanical when no model is available. A
    proposal the human has to rewrite is still far better than a proposal that
    sounds fluent and says the wrong thing, and the review step expects to be
    edited.
    """
    subjects = sorted(subject_entities(statement))
    losing = ", ".join(subjects[:2]) or "the previous choice"
    proposal = Proposal(statement=statement, text="", rationale="", evidence=list(evidence))

    winners = proposal.winners
    if phrasing:
        proposal.text = phrasing
    elif winners:
        proposal.text = (
            f"Prefers {winners[0]} over {losing}; the last {len(evidence)} decisions went that way"
        )
    else:
        proposal.text = f"No longer prefers {losing}"

    projects = {str(item.record.project_id) for item in evidence}
    proposal.rationale = (
        f"{len(evidence)} contradicting records across {len(projects)} project(s), "
        f"by the {', '.join(sorted({item.rule for item in evidence}))} rule"
    )
    proposal.impact = round(
        min(1.0, statement.confidence * (0.5 + 0.15 * len(evidence)) + 0.1 * len(projects)), 4
    )
    return proposal


def entity_index(records: Iterable[EpisodicRecord]) -> dict[str, list[EpisodicRecord]]:
    """Public helper, used by the graph to avoid rebuilding the prefilter."""
    index: dict[str, list[EpisodicRecord]] = {}
    for record in records:
        for entity in record.entities:
            index.setdefault(entity, []).append(record)
    return index
