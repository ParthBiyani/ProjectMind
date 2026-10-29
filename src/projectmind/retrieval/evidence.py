"""Finding what the user's own files and notes say about a question.

The contract, in order of importance:

1. **Grounded.** What is served is an excerpt copied from a source, with the exact
   place it came from. Nothing is summarised, paraphrased or inferred.
2. **Relevant or silent.** A passage is served only when it contains a large
   enough share of the question's *distinctive* terms, including its most
   specific one. A passage that merely shares common words is not evidence, and
   a question about something the corpus never mentions gets nothing, not the
   least-bad match.
3. **Useful when it can be.** Terms are weighted by rarity, so a question with
   one rare, specific term ("ADX") is answered by the passages that say it, even
   if the rest of the question is ordinary English.

The measure is *coverage*: the rarity-weighted share of the question's terms
that appear in the passage (or in the name of the project and document it lives
in). A term the corpus has never seen counts against every passage, which is
what makes a question about an unknown topic fail the floor instead of being
answered with a nearby, unrelated passage.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from projectmind.config import Settings, get_settings
from projectmind.logging import get_logger
from projectmind.models import ServedEvidence
from projectmind.storage import ChunkHit, Store

log = get_logger(__name__)

#: Words that carry no topic. The rarity weighting already discounts anything
#: common across the corpus, but a filler verb like "handle" can be rare enough
#: to look important, so the obvious ones are removed outright.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "when",
        "while",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "done",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "i",
        "me",
        "my",
        "we",
        "our",
        "us",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "not",
        "no",
        "yes",
        "so",
        "than",
        "too",
        "very",
        "just",
        "also",
        "only",
        "any",
        "all",
        "some",
        "each",
        "every",
        "more",
        "most",
        "other",
        "such",
        "own",
        "same",
        "please",
        "help",
        "need",
        "needs",
        "want",
        "wants",
        "like",
        "get",
        "got",
        "make",
        "makes",
        "made",
        "use",
        "uses",
        "used",
        "using",
        "let",
        "lets",
        "add",
        "adds",
        "fix",
        "fixes",
        "new",
        "old",
        "one",
        "two",
        "out",
        "up",
        "down",
        "off",
        "again",
        "further",
        "once",
        "code",
        "file",
        "files",
        "function",
        "functions",
        "project",
        "projects",
        "thing",
        "things",
        "way",
        "ways",
        "something",
        "anything",
        "handle",
        "handles",
        "handling",
        "set",
        "sets",
        "setup",
        "work",
        "works",
        "working",
        "build",
        "builds",
        "create",
        "creates",
        "implement",
        "implements",
        "implementation",
        "write",
        "writes",
        "change",
        "changes",
        "update",
        "updates",
        "improve",
        "improves",
        "good",
        "bad",
        "better",
        "task",
        "tasks",
        "issue",
        "issues",
        "problem",
        "problems",
        "step",
        "steps",
        "example",
        "examples",
        "question",
        "questions",
        "try",
        "tried",
        "trying",
        "look",
        "looks",
        "see",
        "know",
        "think",
        "go",
        "going",
    ]
)

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-+.#]*")

MAX_EXCERPT_LINES = 12
MAX_EXCERPT_CHARS = 1_000
CANDIDATES = 40
PROJECT_CANDIDATES = 15
PER_DOCUMENT = 2

#: A term in more than this share of all chunks says nothing about the topic.
COMMON_SHARE = 0.25
COMMON_MIN_CORPUS = 200

#: The user is working in the project the passage belongs to.
SAME_PROJECT_BOOST = 1.15
#: The question names the project the passage belongs to ("the prostate MRI task").
NAMED_PROJECT_BOOST = 1.3
#: An absent term weighs at most this many times a term seen once.
MISSING_TERM_CAP = 1.25
#: Two passages sharing this much of their vocabulary are the same passage.
NEAR_DUPLICATE = 0.75


def question_terms(prompt: str) -> list[str]:
    """The distinctive words of a question, in order, without duplicates.

    Hyphenated and dotted names are split (`walk-forward` -> `walk`, `forward`)
    because the index tokenises that way; two-character terms are kept only when
    they look like names (`F1`, `CV`, a number) and not like fragments.
    """
    seen: dict[str, None] = {}
    for raw in _TOKEN.findall(prompt):
        for piece in re.split(r"[\-_.+#]+", raw):
            lowered = piece.lower()
            if not lowered or lowered in STOPWORDS:
                continue
            if len(lowered) == 2 and not (any(ch.isdigit() for ch in lowered) or piece.isupper()):
                continue
            if len(lowered) < 2:
                continue
            seen[lowered] = None
    return list(seen)


def _chunk_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in _TOKEN.findall(text):
        for piece in re.split(r"[\-_.+#]+", raw):
            if piece:
                terms.add(piece.lower())
    return terms


def name_terms(name: str) -> set[str]:
    """The words in a project key or document title."""
    return {piece for piece in re.split(r"[^a-z0-9]+", name.lower()) if len(piece) >= 2}


def _close(left: str, right: str) -> bool:
    """Two words that are the same word in different inflections.

    `return`/`returns`, `value`/`values`, `optimize`/`optimization`. Short words
    must match exactly: `run` is not `running`, and `cat` is not `catalog`.
    """
    shortest = min(len(left), len(right))
    if shortest < 4 or abs(len(left) - len(right)) > 4:
        return False
    prefix = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        prefix += 1
    return prefix >= shortest - 1


def contains_term(term: str, vocabulary: set[str]) -> bool:
    """Whether a passage's vocabulary has this word, in any inflection."""
    if term in vocabulary:
        return True
    if len(term) < 4:
        return False
    return any(word[:1] == term[:1] and _close(term, word) for word in vocabulary)


def idf(total: int, frequency: int) -> float:
    """Rarity weight. A term never seen gets the largest weight there is."""
    return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))


@dataclass(frozen=True, slots=True)
class Scored:
    hit: ChunkHit
    coverage: float
    matched: tuple[str, ...]
    score: float
    vocabulary: frozenset[str]


@dataclass(slots=True)
class Selection:
    """What has been chosen so far, for judging the next candidate."""

    limit: int
    anchor: str
    needed: int
    floor: float
    chosen: list[Scored]
    per_document: dict[str, int]


class EvidenceRetriever:
    """Answers "what do my own sources say about this?" with cited excerpts."""

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()

    def retrieve(
        self,
        prompt: str,
        *,
        project_key: str | None = None,
        limit: int | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> list[ServedEvidence]:
        limit = limit or self.settings.max_evidence
        terms = question_terms(prompt)
        total = self.store.count_chunks()
        if not terms or total == 0:
            return []

        frequency = self.store.term_document_frequency(terms)
        weights = self._weights(terms, frequency, total)
        present = [term for term in weights if frequency.get(term, 0) > 0]
        if not present:
            self._note(trace, "terms", terms=terms, reason="nothing in the corpus says any of them")
            return []

        named = self._named_projects(terms)
        hits = self.store.search_chunks(present, limit=CANDIDATES)
        hits = self._with_named_projects(hits, present, named)
        scored = [self._score(hit, weights, project_key, named) for hit in hits]
        scored.sort(key=lambda item: item.score, reverse=True)

        selection = Selection(
            limit=limit,
            # The question's most specific word has to be in the passage. This is
            # what stops a passage that only shares generic words from passing.
            anchor=max(present, key=lambda term: weights[term]),
            needed=min(2, len(present)),
            floor=self.settings.evidence_coverage_floor,
            chosen=[],
            per_document={},
        )
        for item in scored:
            outcome = self._judge(item, selection)
            if trace is not None and (outcome == "served" or len(trace) < 14):
                trace.append(
                    {
                        "stage": "evidence",
                        "chunk": str(item.hit.chunk.id),
                        "project": item.hit.document.project_key,
                        "uri": item.hit.document.uri,
                        "coverage": round(item.coverage, 3),
                        "matched": list(item.matched),
                        "score": round(item.score, 3),
                        "outcome": outcome,
                    }
                )
            if outcome == "served":
                selection.chosen.append(item)
                uri = item.hit.document.uri
                selection.per_document[uri] = selection.per_document.get(uri, 0) + 1

        return [self._serve(item, weights, project_key) for item in selection.chosen]

    # --- scoring -----------------------------------------------------------
    def _weights(
        self, terms: Sequence[str], frequency: dict[str, int], total: int
    ) -> dict[str, float]:
        """Rarity weight per term, with uselessly common terms removed."""
        weights: dict[str, float] = {}
        for term in terms:
            seen_in = frequency.get(term, 0)
            if total >= COMMON_MIN_CORPUS and seen_in / total > COMMON_SHARE:
                continue
            weight = idf(total, seen_in)
            if seen_in == 0:
                # A term the corpus has never seen counts against every passage,
                # but not unboundedly: in a small corpus an absent word would
                # otherwise outweigh everything that is actually there.
                weight = min(weight, MISSING_TERM_CAP * idf(total, 1))
            weights[term] = weight
        return weights

    def _named_projects(self, terms: Sequence[str]) -> frozenset[str]:
        """Projects whose name the question uses ("the prostate MRI task")."""
        asked = set(terms)
        found: set[str] = set()
        for key in self.store.document_projects():
            words = name_terms(key)
            overlap = asked & words
            if len(overlap) >= 2 or (len(overlap) == 1 and len(words) <= 2):
                found.add(key)
        return frozenset(found)

    def _with_named_projects(
        self, hits: list[ChunkHit], present: Sequence[str], named: frozenset[str]
    ) -> list[ChunkHit]:
        """Add the best passages of any project the question names.

        That project's notes may never repeat the words of its own name, so a
        text search alone would rank a generic document above them.
        """
        known = {hit.chunk.id for hit in hits}
        for key in sorted(named):
            for extra in self.store.search_chunks(
                present, limit=PROJECT_CANDIDATES, project_key=key
            ):
                if extra.chunk.id not in known:
                    known.add(extra.chunk.id)
                    hits.append(extra)
        return hits

    def _score(
        self,
        hit: ChunkHit,
        weights: dict[str, float],
        project_key: str | None,
        named: frozenset[str],
    ) -> Scored:
        vocabulary = _chunk_terms(f"{hit.chunk.heading} {hit.chunk.text}")
        # Where a passage lives is part of what it says: the project and the
        # document it belongs to.
        present = vocabulary | name_terms(hit.document.project_key) | name_terms(hit.document.title)
        matched = tuple(term for term in weights if contains_term(term, present))
        mass = sum(weights.values())
        coverage = sum(weights[term] for term in matched) / mass if mass else 0.0
        score = coverage
        if hit.document.project_key in named:
            score *= NAMED_PROJECT_BOOST
        if project_key and hit.document.project_key == project_key:
            score *= SAME_PROJECT_BOOST
        return Scored(
            hit=hit,
            coverage=coverage,
            matched=matched,
            score=score,
            vocabulary=frozenset(vocabulary),
        )

    @staticmethod
    def _judge(item: Scored, selection: Selection) -> str:
        if item.coverage < selection.floor:
            return "cut: below the coverage floor"
        if selection.anchor not in item.matched:
            return f"cut: does not mention '{selection.anchor}'"
        if len(item.matched) < selection.needed:
            return "cut: too few of the question's terms"
        if len(selection.chosen) >= selection.limit:
            return "cut: over the limit"
        if selection.per_document.get(item.hit.document.uri, 0) >= PER_DOCUMENT:
            return "cut: this document already contributed"
        for other in selection.chosen:
            union = item.vocabulary | other.vocabulary
            if union and len(item.vocabulary & other.vocabulary) / len(union) >= NEAR_DUPLICATE:
                return "cut: near-duplicate of a served passage"
        return "served"

    @staticmethod
    def _note(trace: list[dict[str, Any]] | None, stage: str, **fields: Any) -> None:
        if trace is not None:
            trace.append({"stage": stage, **fields})

    # --- presentation ------------------------------------------------------
    def _serve(
        self, item: Scored, weights: dict[str, float], project_key: str | None
    ) -> ServedEvidence:
        hit = item.hit
        excerpt, start, end = best_excerpt(hit, set(weights))
        if start:
            citation = f"{hit.document.uri}:{start}" + (f"-{end}" if end != start else "")
        elif hit.chunk.locator:
            citation = f"{hit.document.uri} ({hit.chunk.locator})"
        else:
            citation = hit.document.uri
        return ServedEvidence(
            chunk_id=hit.chunk.id,
            project_key=hit.document.project_key,
            source_type=hit.document.source_type,
            title=hit.document.title,
            heading=hit.chunk.heading,
            excerpt=excerpt,
            citation=citation,
            score=round(item.score, 4),
            matched=item.matched,
            cross_project=bool(project_key and hit.document.project_key != project_key),
        )


def best_excerpt(hit: ChunkHit, terms: set[str]) -> tuple[str, int, int]:
    """The passage to show, as exact lines of the chunk, and its source span.

    A chunk that fits is shown whole. A longer one is cut to the window of
    consecutive lines that contains the most of the question's terms. Lines are
    never altered, so the text shown is exactly the text at the cited span.
    """
    lines = hit.chunk.text.splitlines()
    first = hit.chunk.start_line
    if len(lines) <= MAX_EXCERPT_LINES and len(hit.chunk.text) <= MAX_EXCERPT_CHARS:
        return hit.chunk.text, first, hit.chunk.end_line if first else 0

    scores = [len(_chunk_terms(line) & terms) for line in lines]
    best, best_at = -1, 0
    for begin in range(0, max(1, len(lines) - MAX_EXCERPT_LINES + 1)):
        window = sum(scores[begin : begin + MAX_EXCERPT_LINES])
        if window > best:
            best, best_at = window, begin
    chosen = lines[best_at : best_at + MAX_EXCERPT_LINES]
    while len("\n".join(chosen)) > MAX_EXCERPT_CHARS and len(chosen) > 1:
        # Drop whichever end is less relevant until it fits.
        if scores[best_at] <= scores[best_at + len(chosen) - 1]:
            chosen = chosen[1:]
            best_at += 1
        else:
            chosen = chosen[:-1]
    text = "\n".join(chosen)
    if not first:
        return text, 0, 0
    return text, first + best_at, first + best_at + len(chosen) - 1
