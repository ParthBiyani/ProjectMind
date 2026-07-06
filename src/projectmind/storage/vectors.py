"""Vector encoding and similarity.

Vectors are stored as little-endian float32 blobs. That is the same layout both
backends use, so an embedding written by one is readable by the other and a
migration between them does not mean recomputing every embedding.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

_FLOAT_SIZE = 4


def encode_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *(float(value) for value in vector))


def decode_vector(blob: bytes | memoryview | None) -> list[float]:
    if not blob:
        return []
    data = bytes(blob)
    count = len(data) // _FLOAT_SIZE
    return list(struct.unpack(f"<{count}f", data[: count * _FLOAT_SIZE]))


def l2_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def normalise(vector: Sequence[float]) -> list[float]:
    norm = l2_norm(vector)
    return [value / norm for value in vector] if norm else list(vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, clamped to [-1, 1] and zero for degenerate input."""
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = sum(left[i] * right[i] for i in range(size))
    denominator = l2_norm(left[:size]) * l2_norm(right[:size])
    if denominator == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / denominator))
