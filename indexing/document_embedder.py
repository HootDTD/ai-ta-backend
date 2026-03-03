from __future__ import annotations

"""OpenAI embedding wrapper for the AI-TA indexing pipeline.

Uses text-embedding-3-large (3072 dims) by default, configurable via env vars.
Does NOT depend on chonkie or any ML library — calls OpenAI directly.
"""

import os
from typing import Optional

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()
    return _client


def embed_text(
    text: str,
    model: Optional[str] = None,
    dim: Optional[int] = None,
) -> list[float]:
    """Embed a single text string and return the vector as a list of floats.

    Truncates input to 8000 chars to stay within model token limits.
    """
    model = model or os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    dim = dim or int(os.getenv("EMBEDDING_DIM", "3072"))
    text = text[:8000]  # Safe truncation; embedder silently truncates at token limit anyway
    resp = _get_client().embeddings.create(model=model, input=[text], dimensions=dim)
    return resp.data[0].embedding
