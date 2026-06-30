import math

from apollo.clarification.embedding import (
    CandidateEmbeddingCache,
    candidate_set_hash,
    candidate_surface_texts,
    cosine,
)
from apollo.resolution.candidates import Candidate


def _cand(key, display, aliases=(), exact=()):
    return Candidate(
        canonical_key=key, canon_key=1, node_type="condition", is_misconception=False,
        symbolic=None, aliases=aliases, display_name=display, opposes_key=None, exact_aliases=exact,
    )


def test_surface_texts_dedupes_and_drops_empty():
    c = _cand("k", "Pressure rises", aliases=("Pressure rises", "p up"), exact=("",))
    assert candidate_surface_texts(c) == ("Pressure rises", "p up")


def test_candidate_set_hash_is_stable_and_sensitive():
    a = (_cand("k", "x"),)
    assert candidate_set_hash(a) == candidate_set_hash((_cand("k", "x"),))
    assert candidate_set_hash(a) != candidate_set_hash((_cand("k", "y"),))


def test_cosine_basic():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cache_embeds_each_surface_once_and_memoizes():
    calls = {"n": 0}

    def stub(texts):
        calls["n"] += 1
        return [[float(len(t)), 1.0] for t in texts]

    cands = (_cand("k1", "abc"), _cand("k2", "de", aliases=("fff",)))
    cache = CandidateEmbeddingCache()
    v1 = cache.vectors_for(cands, embedder=stub)
    v2 = cache.vectors_for(cands, embedder=stub)  # memoized -> no second embed
    assert calls["n"] == 1
    assert set(v1) == {"k1", "k2"}
    assert len(v1["k2"]) == 2  # "de" + "fff"
    assert v1 == v2
