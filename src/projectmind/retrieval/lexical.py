"""BM25 over episodic memory.

Written out rather than pulled in, for two reasons. It is about eighty lines,
and the index has to be built over a field mix that no library knows about:
what happened, why, the outcome, the rejected alternatives, and the entity list
weighted more heavily than the prose.

BM25 is half of the hybrid. It is very good at the thing vectors are bad at —
matching a rare exact token like `CTC` or `num_workers` — and poor at the thing
vectors are good at, which is matching a paraphrase. Neither alone is enough,
which is why the results are fused rather than one being chosen.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from projectmind.models import EpisodicRecord
from projectmind.storage.embeddings import tokenize

#: Standard BM25 parameters. k1 controls how quickly term frequency saturates,
#: b how strongly length is normalised. The defaults are the usual ones and
#: there is no evidence in this corpus for moving them.
K1 = 1.5
B = 0.75

#: Entities are repeated this many times when the document is built. They are
#: the curated, unambiguous part of a record, so a query that names one should
#: outrank a query that merely shares prose with it.
ENTITY_WEIGHT = 3


@dataclass(frozen=True, slots=True)
class LexicalHit:
    record_id: UUID
    score: float
    matched_terms: tuple[str, ...]


@dataclass(slots=True)
class BM25Index:
    """An in-memory BM25 index over a set of records.

    Rebuilt per query batch rather than persisted. At a few thousand records
    that is single-digit milliseconds, and an index that is always rebuilt is
    an index that can never be stale — which matters more here than the
    microseconds, because ingestion runs whenever the user feels like it.
    """

    documents: dict[UUID, list[str]] = field(default_factory=dict)
    frequencies: dict[UUID, Counter[str]] = field(default_factory=dict)
    document_frequency: Counter[str] = field(default_factory=Counter)
    average_length: float = 0.0

    @property
    def size(self) -> int:
        return len(self.documents)

    @classmethod
    def build(cls, records: Iterable[EpisodicRecord]) -> BM25Index:
        index = cls()
        total_length = 0
        for record in records:
            tokens = document_tokens(record)
            if not tokens:
                continue
            index.documents[record.id] = tokens
            counts = Counter(tokens)
            index.frequencies[record.id] = counts
            index.document_frequency.update(counts.keys())
            total_length += len(tokens)
        index.average_length = total_length / index.size if index.size else 0.0
        return index

    def idf(self, term: str) -> float:
        """Robertson-Sparck-Jones IDF, floored so common terms cannot go negative."""
        appearances = self.document_frequency.get(term, 0)
        if not appearances:
            return 0.0
        numerator = self.size - appearances + 0.5
        denominator = appearances + 0.5
        return max(0.0, math.log(1.0 + numerator / denominator))

    def search(self, query: str, *, limit: int = 50) -> list[LexicalHit]:
        """Score every document against the query, best first.

        Scores are normalised against the best hit, so a value is "how close to
        the strongest match in this result set" rather than an absolute BM25
        number that means nothing on its own and cannot be fused with a cosine.
        """
        terms = tokenize(query)
        if not terms or not self.size:
            return []

        raw: list[tuple[UUID, float, tuple[str, ...]]] = []
        for record_id, tokens in self.documents.items():
            counts = self.frequencies[record_id]
            length = len(tokens)
            score = 0.0
            matched: list[str] = []
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                matched.append(term)
                idf = self.idf(term)
                denominator = frequency + K1 * (
                    1 - B + B * (length / self.average_length if self.average_length else 1.0)
                )
                score += idf * (frequency * (K1 + 1)) / denominator
            if score > 0.0:
                raw.append((record_id, score, tuple(dict.fromkeys(matched))))

        if not raw:
            return []
        best = max(score for _, score, _ in raw)
        hits = [
            LexicalHit(record_id=record_id, score=score / best, matched_terms=matched)
            for record_id, score, matched in raw
        ]
        hits.sort(key=lambda hit: -hit.score)
        return hits[:limit]


def document_tokens(record: EpisodicRecord) -> list[str]:
    """The token stream one record contributes to the index.

    Hyphenated entities are indexed whole *and* split. The tokenizer keeps
    hyphens so that `scikit-learn` survives as one term, which means
    `state-management` is a single token and the query word "state" cannot
    reach it. Indexing both forms costs two tokens and fixes a class of miss
    where the developer types the words and memory has the slug.
    """
    tokens = tokenize(record.searchable_text)
    for entity in record.entities:
        forms = tokenize(entity)
        forms.extend(part for part in re.split(r"[-_.]", entity) if len(part) > 1)
        tokens.extend(list(dict.fromkeys(forms)) * ENTITY_WEIGHT)
    tokens.append(str(record.type))
    return tokens


def search_records(
    records: Sequence[EpisodicRecord], query: str, *, limit: int = 50
) -> list[LexicalHit]:
    """Convenience wrapper: build an index over `records` and query it once."""
    return BM25Index.build(records).search(query, limit=limit)
