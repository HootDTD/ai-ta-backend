def test_primitives_importable_from_resolution():
    from apollo.resolution.embedding import (  # noqa: F401
        CandidateEmbeddingCache,
        Embedder,
        candidate_set_hash,
        candidate_surface_texts,
        cosine,
        default_embedder,
    )

    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
