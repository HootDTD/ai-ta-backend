"""Deterministic fake embeddings.

`fake_embedding(text)` returns a unit-norm vector that is a pure function of
the input text — same text always yields the same vector, different texts
yield different vectors. This lets retrieval tests assert *ordering* and
*distance* behaviour without calling the real embedding API.

`one_hot_embedding(i)` returns an axis-aligned unit vector, useful when a test
needs vectors with exactly-known pairwise distances (orthogonal one-hots have
cosine distance 1.0; identical ones have 0.0).

Dimension matches the production model (`EMBEDDING_DIM`, default 3072 for
text-embedding-3-large) so the vectors are insertable into the real
`Vector(EMBEDDING_DIM)` columns.
"""

from __future__ import annotations

import hashlib
import math
import os
import random

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))


def _seed_from_text(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic unit-norm pseudo-random embedding for ``text``."""
    rng = random.Random(_seed_from_text(text))
    vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def one_hot_embedding(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """Unit vector with 1.0 at ``index`` (mod ``dim``), 0.0 elsewhere.

    Use when a test needs deterministic, hand-computable distances between
    vectors rather than the opaque distances of :func:`fake_embedding`.
    """
    if dim <= 0:
        raise ValueError("dim must be positive")
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec
