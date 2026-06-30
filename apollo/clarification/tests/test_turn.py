from apollo.clarification import turn
from apollo.clarification.embedding import CandidateEmbeddingCache
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node


def _cand(key, display, node_type="condition"):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=display,
        opposes_key=None,
        exact_aliases=(),
    )


async def test_detection_returns_hints_and_persists(monkeypatch):
    writes = []

    async def fake_write(db, **kw):
        writes.append(kw)

    monkeypatch.setattr(turn, "write_asked_waiting", fake_write)

    node = _node("s1", "condition", {"applies_when": "pressure and speed related", "label": ""})
    cand = _cand("cond.bernoulli", "pressure lower where faster")

    def emb(texts):
        # student text and candidate surface both ~ [1,0] -> high cosine.
        return [[1.0, 0.0] for _ in texts]

    hints = await turn.run_clarification_detection(
        db=object(),
        parsed_nodes=[node],
        candidates=(cand,),
        symbolic_mappings={},
        embedder=emb,
        cache=CandidateEmbeddingCache(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=2,
    )
    assert hints and "direction" in hints[0].lower()
    assert len(writes) == 1
    assert writes[0]["node_id"] == "s1"
    assert writes[0]["candidate_key"] == "cond.bernoulli"


async def test_detection_failsafe_returns_empty(monkeypatch):
    """When something inside the try block raises (e.g. find_residual_nodes
    fails), run_clarification_detection's except clause fires and returns []."""

    def boom(nodes, candidates, **kwargs):
        raise RuntimeError("503")

    monkeypatch.setattr(turn, "find_residual_nodes", boom)

    hints = await turn.run_clarification_detection(
        db=object(),
        parsed_nodes=[_node("s1", "condition", {"applies_when": "x", "label": ""})],
        candidates=(_cand("k", "d"),),
        symbolic_mappings={},
        embedder=lambda texts: [[1.0, 0.0] for _ in texts],
        cache=CandidateEmbeddingCache(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=2,
    )
    assert hints == []


async def test_detection_empty_inputs_returns_early():
    """Empty parsed_nodes triggers the early-exit guard (line 40)."""
    hints = await turn.run_clarification_detection(
        db=object(),
        parsed_nodes=[],
        candidates=(_cand("k", "d"),),
        symbolic_mappings={},
        embedder=lambda texts: [[1.0, 0.0] for _ in texts],
        cache=CandidateEmbeddingCache(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=2,
    )
    assert hints == []
