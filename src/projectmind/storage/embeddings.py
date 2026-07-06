"""Embeddings.

Two providers, one interface. The default is a deterministic hashing embedder
that needs no model, no download and no network, because the alternative is a
system that silently has no vector search on a fresh machine. It is not as good
as a trained encoder, and the hybrid retriever is built on the assumption that
it is only half the signal; BM25 is the other half.

Set `PROJECTMIND_EMBEDDING_PROVIDER=fastembed` (and install
`projectmind[embeddings]`) to use a real ONNX sentence encoder instead. The
stored format is identical, so the only cost of switching is re-embedding.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

from projectmind.config import Settings, get_settings
from projectmind.logging import get_logger

log = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+-]*")
_CHAR_NGRAM = 4


class EmbeddingProvider(Protocol):
    """Anything that can turn text into a fixed-width vector."""

    name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, keeping dots and dashes so `scikit-learn` survives."""
    return _TOKEN.findall(text.lower())


def _signed_bucket(feature: str, dimensions: int) -> tuple[int, float]:
    """Map a feature to a bucket and a sign.

    The sign is what stops unrelated features that collide into the same bucket
    from always reinforcing each other; half the collisions cancel instead.
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dimensions, 1.0 if (value >> 63) & 1 else -1.0


class HashingEmbedder:
    """Deterministic feature-hashing embedder. No model, no network, no state.

    Features are word unigrams, word bigrams and character 4-grams. Term
    frequency is damped with `1 + log(tf)` so a record that repeats a word
    twenty times does not drown out everything else, and the result is L2
    normalised so cosine similarity is a plain dot product.
    """

    name = "hashing"

    def __init__(self, dimensions: int = 384) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        counts: dict[str, int] = {}
        tokens = tokenize(text)
        for token in tokens:
            counts[f"w:{token}"] = counts.get(f"w:{token}", 0) + 1
            for start in range(len(token) - _CHAR_NGRAM + 1):
                gram = token[start : start + _CHAR_NGRAM]
                counts[f"c:{gram}"] = counts.get(f"c:{gram}", 0) + 1
        for left, right in itertools.pairwise(tokens):
            key = f"b:{left}_{right}"
            counts[key] = counts.get(key, 0) + 1

        vector = [0.0] * self.dimensions
        for feature, count in counts.items():
            bucket, sign = _signed_bucket(feature, self.dimensions)
            vector[bucket] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class FastEmbedProvider:
    """ONNX sentence encoder via `fastembed`. Better vectors, optional install."""

    name = "fastembed"

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", dimensions: int = 384) -> None:
        try:
            from fastembed import TextEmbedding
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "the fastembed provider needs fastembed; install projectmind[embeddings]"
            ) from exc
        self._model = TextEmbedding(model_name=model)
        self.dimensions = dimensions
        self.model_name = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(value) for value in vector] for vector in self._model.embed(list(texts))]


def build_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    """Resolve the configured provider, falling back rather than failing.

    `auto` prefers fastembed when it is importable and drops to hashing
    otherwise. A provider that cannot be constructed is a degraded search, not
    a dead server, so the fallback is logged and taken.
    """
    settings = settings or get_settings()
    choice = settings.embedding_provider.lower()
    dimensions = settings.embedding_dimensions

    if choice in {"auto", "fastembed"}:
        try:
            return FastEmbedProvider(dimensions=dimensions)
        except Exception as exc:
            if choice == "fastembed":
                log.warning("fastembed unavailable, falling back", extra={"error": str(exc)})
            else:
                log.debug("fastembed not installed, using the hashing embedder")
    return HashingEmbedder(dimensions=dimensions)


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingProvider:
    """Process-wide embedder. Loading an ONNX model twice is pure waste."""
    return build_embedder()


def reset_embedder() -> None:
    get_embedder.cache_clear()


def embed_one(text: str, provider: EmbeddingProvider | None = None) -> list[float]:
    return (provider or get_embedder()).embed([text])[0]
