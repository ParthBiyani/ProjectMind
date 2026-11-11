"""A random sample of passages, for writing evaluation questions that the retriever never shaped.

The usual way to label an evaluation set is to ask a question, look at what the retriever
returns and mark what looks right. That bakes the retriever's own taste into the answer
key. This works the other way round: draw passages at random (seeded, so it can be
repeated), and write a question each passage answers *before* running anything. The label
is then the passage's own file, fixed by the draw rather than by the system under test.

Everything printed has been through redaction, and identity-document names, web pages and
commit links are never drawn.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from projectmind.sources.secrets import redact_text
from projectmind.storage import ChunkRecord, DocumentRecord, Store

#: Source types worth asking questions about. Web pages are titles, commits are records.
DRAWN_TYPES = ("doc", "code", "notebook", "pdf", "docx", "pptx", "log", "results", "transcript")
MIN_CHARS = 300
MAX_CHARS = 3000


@dataclass(frozen=True, slots=True)
class Sample:
    document: DocumentRecord
    chunk: ChunkRecord

    @property
    def label(self) -> str:
        """The tail of the file's path, which survives re-indexing and renaming a root."""
        parts = PurePosixPath(self.document.uri).parts
        return "/".join(parts[-2:]) if len(parts) > 1 else self.document.uri

    @property
    def folder(self) -> str:
        return str(PurePosixPath(self.document.uri).parent)

    @property
    def excerpt(self) -> str:
        return redact_text(self.chunk.text.strip())


def draw(
    store: Store,
    count: int,
    *,
    seed: int,
    exclude: Iterable[str] = (),
    types: Iterable[str] = DRAWN_TYPES,
) -> list[Sample]:
    """`count` passages, spread across source types, the same ones for the same seed.

    `exclude` holds substrings of a document's address to leave out (a private folder, the
    project being evaluated). Each type gets an equal share first, so a thousand code files
    cannot crowd out the handful of PDFs.
    """
    skip = [part.lower() for part in exclude]
    allowed = set(types)
    documents = {
        doc.id: doc
        for doc in store.list_documents(limit=1_000_000)
        if doc.source_type in allowed and not any(part in doc.uri.lower() for part in skip)
    }
    pools: dict[str, list[Sample]] = defaultdict(list)
    for chunk in store.iter_chunks():
        document = documents.get(chunk.document_id)
        if document is None or not MIN_CHARS <= len(chunk.text.strip()) <= MAX_CHARS:
            continue
        pools[document.source_type].append(Sample(document, chunk))

    rng = random.Random(seed)
    kinds = sorted(pools)
    for kind in kinds:
        pools[kind].sort(key=lambda s: (s.document.uri, s.chunk.ordinal))  # stable before shuffle
        rng.shuffle(pools[kind])

    drawn: list[Sample] = []
    seen_documents: set[str] = set()
    while len(drawn) < count and any(pools[k] for k in kinds):
        for kind in kinds:
            while pools[kind]:
                candidate = pools[kind].pop()
                if candidate.document.uri in seen_documents:
                    continue  # one passage per file, so one file cannot be asked about twice
                seen_documents.add(candidate.document.uri)
                drawn.append(candidate)
                break
            if len(drawn) >= count:
                break
    return drawn


def skeleton(samples: Iterable[Sample], *, limit: int = 700) -> str:
    """YAML to fill in: one entry per passage, the excerpt as comments, the prompt blank."""
    lines = [
        "# Fill in each `prompt` with a question this passage answers, written as you would ask",
        "# it, before running `projectmind eval real`. Keep `any_of`; delete entries you cannot",
        "# write a good question for rather than bending the question to the passage.",
        "name: blind",
        "queries:",
    ]
    for number, sample in enumerate(samples, start=1):
        excerpt = " ".join(sample.excerpt.split())[:limit]
        lines += [
            f"  # {sample.document.source_type} · {sample.label} · {sample.chunk.locator or ''}",
            f"  # {excerpt}",
            f"  - id: b{number:02d}",
            '    prompt: "TODO"',
            f'    project: "{sample.folder}"',
            f'    any_of: ["{sample.label}"]',
            "",
        ]
    return "\n".join(lines)
