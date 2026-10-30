"""Matching a question to passages by meaning, not only by shared words.

Lexical search is exact, cited and never invents a connection, but it cannot see
that "heart cycle" and "Cardiac Cycle" are the same thing. This adds the missing
half without giving up the rest:

* every passage is embedded once, in the background, and the vectors are stored;
* a daemon keeps the matrix in memory, where a search is a few milliseconds;
* a semantic match is served only above a calibrated similarity, so that a
  question about something you never wrote about still returns nothing.

It is optional (`pip install 'projectmind[semantic]'`, then `projectmind semantic
enable`). Without it, or in a hook that has no daemon to ask, retrieval is the
lexical retrieval it always was.

numpy and the model are imported lazily; nothing here costs a hook anything unless
semantic search is actually used.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from projectmind.logging import get_logger
from projectmind.storage import Store

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

log = get_logger(__name__)

MODEL = "BAAI/bge-small-en-v1.5"
MAX_CHARS = 1_200
BATCH = 64
PAGE = 1_000


class Encoder(Protocol):
    """Turns text into unit-length vectors."""

    def embed(self, texts: Sequence[str]) -> Any: ...

    def embed_query(self, text: str) -> Any: ...


class FastEmbedEncoder:
    """The default: a small ONNX sentence model that runs on a CPU."""

    def __init__(self, model_name: str = MODEL) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name)

    def embed(self, texts: Sequence[str]) -> Any:
        import numpy as np

        vectors = np.array(list(self._model.embed(list(texts), batch_size=BATCH)), dtype="float32")
        return _unit(vectors)

    def embed_query(self, text: str) -> Any:
        import numpy as np

        vector = np.array(next(iter(self._model.query_embed([text]))), dtype="float32")
        return _unit(vector[None, :])[0]


def _unit(vectors: Any) -> Any:
    import numpy as np

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-9)


def available() -> bool:
    """Whether the optional dependencies are installed."""
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def passage_text(heading: str, text: str) -> str:
    return f"{heading}\n{text}"[:MAX_CHARS]


@dataclass(frozen=True, slots=True)
class SemanticHit:
    chunk_id: UUID
    similarity: float


class SemanticIndex:
    """Stored vectors, an in-memory matrix and a nearest-passages search."""

    def __init__(
        self,
        store: Store,
        *,
        model_name: str = MODEL,
        encoder: Encoder | None = None,
        encoder_factory: Callable[[], Encoder] | None = None,
    ) -> None:
        self.store = store
        self.model_name = model_name
        self._encoder = encoder
        self._factory = encoder_factory or (lambda: FastEmbedEncoder(model_name))
        self._ids: list[UUID] = []
        self._matrix: np.ndarray[Any, Any] | None = None

    # --- encoder ------------------------------------------------------------
    @property
    def encoder(self) -> Encoder:
        if self._encoder is None:
            self._encoder = self._factory()
        return self._encoder

    @property
    def size(self) -> int:
        return len(self._ids)

    # --- building -------------------------------------------------------------
    def pending(self) -> int:
        """How many passages have no vector yet."""
        total = self.store.count_chunks()
        return max(0, total - self.store.count_chunk_vectors(self.model_name))

    def embed_pending(
        self,
        *,
        limit: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Embed passages that have no vector. Returns how many were done."""
        import numpy as np

        done = 0
        target = self.pending() if limit is None else min(limit, self.pending())
        started = time.perf_counter()
        while done < target:
            batch = self.store.chunks_without_vectors(self.model_name, min(PAGE, target - done))
            if not batch:
                break
            vectors = self.encoder.embed([passage_text(c.heading, c.text) for c in batch])
            blobs = [np.asarray(row, dtype="float16").tobytes() for row in vectors]
            self.store.put_chunk_vectors(
                self.model_name, [(c.id, b) for c, b in zip(batch, blobs, strict=True)]
            )
            done += len(batch)
            if progress:
                progress(done, target)
        log.info(
            "embedded passages",
            extra={"count": done, "seconds": round(time.perf_counter() - started, 1)},
        )
        return done

    # --- searching ----------------------------------------------------------
    def load(self) -> int:
        """Read the stored vectors into memory. Returns how many."""
        import numpy as np

        ids: list[UUID] = []
        rows: list[bytes] = []
        for chunk_id, blob in self.store.iter_chunk_vectors(self.model_name):
            ids.append(chunk_id)
            rows.append(blob)
        if not rows:
            self._ids, self._matrix = [], None
            return 0
        matrix = np.vstack([np.frombuffer(blob, dtype="float16") for blob in rows])
        self._matrix = matrix.astype("float32")
        self._ids = ids
        return len(ids)

    def search(self, query: str, k: int = 10) -> list[SemanticHit]:
        """The passages nearest in meaning to the query, best first."""
        import numpy as np

        if self._matrix is None or not self._ids:
            return []
        vector = np.asarray(self.encoder.embed_query(query), dtype="float32")
        similarities = self._matrix @ vector
        count = min(k, len(self._ids))
        top = np.argpartition(-similarities, count - 1)[:count]
        top = top[np.argsort(-similarities[top])]
        return [SemanticHit(self._ids[int(i)], float(similarities[int(i)])) for i in top]


def attach(memory: Any) -> SemanticIndex | None:
    """Give a service meaning search, loading the stored vectors. None if unavailable."""
    if not available():
        return None
    index = SemanticIndex(memory.store)
    if not index.load():
        return None
    memory.evidence.semantic = index
    return index
