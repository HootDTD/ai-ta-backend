"""Task 6 — unit tests for detect_ambiguous_nodes (embedding-similarity detector).

The `_node` helper from test_resolver is reused; condition nodes carry their
text in the `applies_when` content field, which `student_surface_text` returns.
The `_cand` helper below mirrors test_embedding's pattern (Candidate factory).

Adapted from the brief: `_node` takes (node_id, node_type, content_dict) —
keyword `text=` does not exist in the actual helper; condition content is
`{"applies_when": "...", "label": ""}`.
"""

from apollo.clarification.detector import FlaggedNode, T_AMBIG, detect_ambiguous_nodes
from apollo.clarification.embedding import CandidateEmbeddingCache
from apollo.resolution.candidates import Candidate
# Reuse the resolution test node builder.
from apollo.resolution.tests.test_resolver import _node


def _cand(key, display):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=display,
        opposes_key=None,
        exact_aliases=(),
    )


def _embedder_for(mapping):
    """Deterministic stub: maps known text -> vector; unknown text -> orthogonal [0,1]."""

    def stub(texts):
        return [mapping.get(t, [0.0, 1.0]) for t in texts]

    return stub


def test_flags_node_in_ambiguous_band_with_top_candidate():
    node = _node("s1", "condition", {"applies_when": "pressure drops when faster", "label": ""})
    cand = _cand("cond.bernoulli", "pressure is lower where flow is faster")
    emb = _embedder_for(
        {
            "pressure drops when faster": [1.0, 0.0],
            "pressure is lower where flow is faster": [0.9, 0.1],  # cosine ~0.994 >= 0.50
        }
    )
    flagged = detect_ambiguous_nodes([node], (cand,), embedder=emb, cache=CandidateEmbeddingCache())
    assert len(flagged) == 1
    assert flagged[0].candidate.canonical_key == "cond.bernoulli"
    assert flagged[0].cosine >= T_AMBIG


def test_leaves_node_below_band():
    node = _node("s1", "condition", {"applies_when": "off topic", "label": ""})
    cand = _cand("cond.bernoulli", "pressure lower where faster")
    emb = _embedder_for({"off topic": [0.0, 1.0], "pressure lower where faster": [1.0, 0.0]})
    assert (
        detect_ambiguous_nodes([node], (cand,), embedder=emb, cache=CandidateEmbeddingCache()) == []
    )


def test_picks_best_surface_when_candidate_has_multiple_surfaces():
    """When a candidate has >1 surface text, the best cosine (not the last)
    is used. Covers the `if c > best_cos: False` branch (non-max surface)."""
    node = _node("s1", "condition", {"applies_when": "query text", "label": ""})
    # Candidate has two aliases: the second is the closer match.
    cand = Candidate(
        canonical_key="cond.x",
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=("unrelated noise",),
        display_name="query text close match",
        opposes_key=None,
        exact_aliases=(),
    )
    emb = _embedder_for(
        {
            "query text": [1.0, 0.0],
            "query text close match": [0.95, 0.05],  # cosine ~0.998 → best_cos updated
            "unrelated noise": [0.0, 1.0],           # cosine 0.0 → False branch fires
        }
    )
    flagged = detect_ambiguous_nodes([node], (cand,), embedder=emb, cache=CandidateEmbeddingCache())
    assert len(flagged) == 1
    assert flagged[0].cosine >= T_AMBIG


def test_empty_inputs_and_embedder_failure_are_no_ops():
    cand = _cand("k", "x")
    # Empty residual_nodes list → immediate [] return (no embed called).
    assert (
        detect_ambiguous_nodes([], (cand,), embedder=lambda t: [], cache=CandidateEmbeddingCache())
        == []
    )

    def boom(texts):
        raise RuntimeError("openai 503")

    node = _node("s1", "condition", {"applies_when": "anything", "label": ""})
    # Embedder raises → fail-safe path → [] (never re-raises).
    assert (
        detect_ambiguous_nodes([node], (cand,), embedder=boom, cache=CandidateEmbeddingCache())
        == []
    )
