"""Embedding primitives for the Apollo resolution layer.

Meaning-matching (cosine over text-embedding-3-large) maps student surface text
onto candidate nodes. Candidate surface embeddings are precomputed and memoized
per candidate-set hash so callers pay one batched embed per turn.

These primitives are intentionally neutral so resolution and clarification
can import from this module without a cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable

from apollo.resolution.candidates import Candidate

Embedder = Callable[[list[str]], list[list[float]]]


def default_embedder(texts: list[str]) -> list[list[float]]:
    """Project-wide text-embedding-3-large path (batched). Lazy-imported so test
    collection never touches the OpenAI SDK."""
    from indexing.document_embedder import embed_texts

    return embed_texts(texts)


def candidate_surface_texts(candidate: Candidate) -> tuple[str, ...]:
    """The texts whose meaning identifies this candidate: display name + aliases
    + exact aliases, order-preserving dedupe, empties dropped."""
    seen: dict[str, None] = {}
    for t in (candidate.display_name, *candidate.aliases, *candidate.exact_aliases):
        t = (t or "").strip()
        if t and t not in seen:
            seen[t] = None
    return tuple(seen)


def candidate_set_hash(candidates: tuple[Candidate, ...]) -> str:
    """Deterministic sha256 over the candidate identity fields — the cache key.
    Tracks the same invalidation surface as the reference (the candidate set is
    derived from the reference + misconceptions)."""
    payload = sorted(
        [c.canonical_key, str(c.node_type), c.display_name, list(c.aliases), list(c.exact_aliases)]
        for c in candidates
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "clarcache-v1:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return num / (na * nb)


class CandidateEmbeddingCache:
    """Memoizes candidate surface embeddings per candidate-set hash."""

    def __init__(self) -> None:
        self._by_hash: dict[str, dict[str, list[list[float]]]] = {}

    def vectors_for(
        self, candidates: tuple[Candidate, ...], *, embedder: Embedder
    ) -> dict[str, list[list[float]]]:
        key = candidate_set_hash(candidates)
        cached = self._by_hash.get(key)
        if cached is not None:
            return cached
        flat_texts: list[str] = []
        spans: list[tuple[str, int, int]] = []
        for c in candidates:
            surfaces = candidate_surface_texts(c)
            start = len(flat_texts)
            flat_texts.extend(surfaces)
            spans.append((c.canonical_key, start, len(flat_texts)))
        vectors = embedder(flat_texts) if flat_texts else []
        result: dict[str, list[list[float]]] = {}
        for canonical_key, start, end in spans:
            result.setdefault(canonical_key, []).extend(vectors[start:end])
        self._by_hash[key] = result
        return result
