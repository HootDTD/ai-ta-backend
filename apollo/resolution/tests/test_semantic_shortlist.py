"""Tests for apollo.resolution.semantic_shortlist.

Covers:
- lexical token-overlap fallback (no embedder);
- embedding mode with a fake embedder;
- deterministic tiebreak (equal score → canonical_key asc);
- early-return guard when candidates is empty (covers the guard line);
- _overlap early-return for empty strings (line 48);
- false branch of ``if s >= best`` in both lexical and embedding inner loops.
"""

from __future__ import annotations

from apollo.resolution.candidates import Candidate
from apollo.resolution.semantic_shortlist import _overlap, shortlist_semantic_candidates


def _cand(key, name):
    return Candidate(key, -1, "definition", False, None, (), name, None, ())


def test_lexical_fallback_ranks_by_overlap(make_def_node):
    node = make_def_node("density stays constant throughout the pipe")
    cands = (
        _cand("def.const_density", "density is constant"),
        _cand("def.unrelated", "energy is conserved"),
    )
    out = shortlist_semantic_candidates(node, cands, top_k=2, embedder=None)
    assert out[0].candidate.canonical_key == "def.const_density"
    assert out[0].source == "lexical"


def test_embedding_mode_uses_fake_embedder(make_def_node):
    node = make_def_node("incompressible flow")
    cands = (_cand("def.incompress", "incompressibility"),)
    fake = lambda texts: [[1.0, 0.0] for _ in texts]  # noqa: E731
    out = shortlist_semantic_candidates(node, cands, top_k=1, embedder=fake)
    assert out[0].source == "embedding" and out[0].score == 1.0


def test_deterministic_tiebreak(make_def_node):
    node = make_def_node("x")
    cands = (_cand("def.b", "x"), _cand("def.a", "x"))
    out = shortlist_semantic_candidates(node, cands, top_k=2, embedder=None)
    assert [c.candidate.canonical_key for c in out] == ["def.a", "def.b"]


def test_empty_candidates_returns_empty(make_def_node):
    """Guard line: ``if not text or not candidates: return []``."""
    node = make_def_node("some concept")
    out = shortlist_semantic_candidates(node, (), embedder=None)
    assert out == []


def test_overlap_empty_string_returns_zero():
    """``_overlap`` line 48: early-return 0.0 when either word-set is empty."""
    assert _overlap("", "any word") == 0.0
    assert _overlap("any word", "") == 0.0


def test_lexical_picks_best_surface_over_weaker_alias(make_def_node):
    """False branch of ``if s >= best`` in the lexical inner loop.

    The candidate has two surfaces (display_name + one alias).  The first
    surface is the good match; the second surface scores lower, exercising the
    branch where ``s < best`` keeps the previous best.
    """
    node = make_def_node("density constant")
    # display_name matches well; alias "zephyr foobar" shares no tokens
    cand = Candidate(
        "def.c", -1, "definition", False, None, ("zephyr foobar",), "density constant", None, ()
    )
    out = shortlist_semantic_candidates(node, (cand,), top_k=1, embedder=None)
    assert out[0].text == "density constant"
    assert out[0].score > 0.0


def test_embedding_picks_best_surface_over_weaker_alias(make_def_node):
    """False branch of ``if s >= best`` in the embedding inner loop.

    The candidate has two surfaces.  The embedder returns orthogonal vectors so
    only the first surface has cosine 1.0; the second has 0.0, exercising the
    branch where a lower-scoring surface is skipped.
    """
    node = make_def_node("incompressible flow")
    cand = Candidate(
        "def.c", -1, "definition", False, None, ("something else",), "incompressible flow", None, ()
    )

    def _varying(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "incompressible" in t else [0.0, 1.0] for t in texts]

    out = shortlist_semantic_candidates(node, (cand,), top_k=1, embedder=_varying)
    assert out[0].text == "incompressible flow"
    assert out[0].score == 1.0
