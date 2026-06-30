"""Apollo clarification loop (G2): embeddings notice -> student confirms."""

from apollo.clarification.embedding import (
    CandidateEmbeddingCache,
    Embedder,
    candidate_set_hash,
    candidate_surface_texts,
    cosine,
    default_embedder,
)

__all__ = [
    "CandidateEmbeddingCache",
    "Embedder",
    "candidate_set_hash",
    "candidate_surface_texts",
    "cosine",
    "default_embedder",
]
