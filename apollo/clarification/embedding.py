"""Re-export shim — embedding primitives now live in apollo.resolution.embedding
(neutral module so the NLI resolver can import them without a clarification cycle)."""

from apollo.resolution.embedding import (  # noqa: F401
    CandidateEmbeddingCache,
    Embedder,
    candidate_set_hash,
    candidate_surface_texts,
    cosine,
    default_embedder,
)
