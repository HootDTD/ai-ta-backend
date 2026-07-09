from __future__ import annotations

"""OpenAI embedding wrapper for the AI-TA indexing pipeline.

Uses text-embedding-3-large (3072 dims) by default, configurable via env vars.
Does NOT depend on chonkie or any ML library — calls OpenAI directly.
"""

import os
import threading
from functools import lru_cache
from typing import Optional

_client = None
_client_lock = threading.Lock()

_EMBED_CACHE_SIZE = int(os.getenv("EMBED_CACHE_SIZE", "256"))


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from openai import OpenAI
                _client = OpenAI()
    return _client


@lru_cache(maxsize=_EMBED_CACHE_SIZE)
def _embed_cached(text: str, model: str, dim: int) -> tuple[float, ...]:
    """Cache-friendly embedding call. Returns a tuple (hashable) for LRU cache."""
    resp = _get_client().embeddings.create(model=model, input=[text], dimensions=dim)
    return tuple(resp.data[0].embedding)


def embed_text(
    text: str,
    model: Optional[str] = None,
    dim: Optional[int] = None,
) -> list[float]:
    """Embed a single text string and return the vector as a list of floats.

    Truncates input to 8000 chars to stay within model token limits.
    Results are cached (LRU, 256 entries) — identical text returns instantly.
    """
    model = model or os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    dim = dim or int(os.getenv("EMBEDDING_DIM", "3072"))
    text = text[:8000]  # Safe truncation; embedder silently truncates at token limit anyway
    return list(_embed_cached(text, model, dim))


# Max inputs per embeddings request. text-embedding-3-large accepts up to 2048
# inputs / ~300k tokens; we stay well under to leave headroom for long chunks.
_MAX_INPUTS_PER_REQUEST = 256


def embed_texts(
    texts: list[str],
    model: Optional[str] = None,
    dim: Optional[int] = None,
) -> list[list[float]]:
    """Embed many texts in batched requests; returns vectors in input order.

    One OpenAI request per up-to-``_MAX_INPUTS_PER_REQUEST`` inputs (vs one
    request per text). Each input is truncated to 8000 chars, matching
    :func:`embed_text`.
    """
    if not texts:
        return []
    model = model or os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    dim = dim or int(os.getenv("EMBEDDING_DIM", "3072"))
    client = _get_client()

    vectors: list[list[float]] = []
    for start in range(0, len(texts), _MAX_INPUTS_PER_REQUEST):
        batch = [t[:8000] for t in texts[start:start + _MAX_INPUTS_PER_REQUEST]]
        resp = client.embeddings.create(model=model, input=batch, dimensions=dim)
        vectors.extend(
            list(item.embedding)
            for item in sorted(resp.data, key=lambda item: item.index)
        )
    return vectors
