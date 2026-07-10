from apollo.clarification import turn
from apollo.clarification.embedding import CandidateEmbeddingCache
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node
from apollo.resolver_v2.incremental_types import IncrementalSnapshot


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


async def test_detection_no_embedding_when_all_resolved(monkeypatch):
    """When find_residual_nodes returns [] (all nodes resolve), detect_ambiguous_nodes
    (embedder) is never called and the function returns [] immediately."""
    monkeypatch.setattr(turn, "find_residual_nodes", lambda *a, **kw: [])

    embed_calls = {"count": 0}

    def counting_embedder(texts):
        embed_calls["count"] += 1
        return [[1.0, 0.0] for _ in texts]

    hints = await turn.run_clarification_detection(
        db=object(),
        parsed_nodes=[_node("s1", "condition", {"applies_when": "x", "label": ""})],
        candidates=(_cand("k", "d"),),
        symbolic_mappings={},
        embedder=counting_embedder,
        cache=CandidateEmbeddingCache(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=2,
    )
    assert hints == []
    assert embed_calls["count"] == 0


def _snapshot(node_credits, gray) -> IncrementalSnapshot:
    return IncrementalSnapshot(
        node_credits=node_credits,
        edge_scores=(),
        node_cov=0.5,
        edge_cov=0.5,
        winning_path_index=0,
        gray=gray,
        pair_count_this_turn=0,
    )


def _patch_write(monkeypatch):
    async def fake_write(db, **kw):
        pass

    monkeypatch.setattr(turn, "write_asked_waiting", fake_write)


def _kwargs(**overrides):
    node = _node("s1", "condition", {"applies_when": "pressure and speed related", "label": ""})
    cand = _cand("cond.bernoulli", "pressure lower where faster")
    base = dict(
        db=object(),
        parsed_nodes=[node],
        candidates=(cand,),
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
    base.update(overrides)
    return base


async def test_all_flags_off_is_byte_identical_v1(monkeypatch):
    """§8.2 row 1/2: with the ranker flags unset, run_clarification_detection
    takes the v1 path even when a snapshot is supplied -- v2_selection.select
    is never called."""
    _patch_write(monkeypatch)
    monkeypatch.delenv("APOLLO_CLARIFICATION_V2_RANKER", raising=False)
    monkeypatch.delenv("APOLLO_RESOLVER_V2", raising=False)
    monkeypatch.delenv("APOLLO_CLARIFICATION_ENABLED", raising=False)

    called = {"v2": False}

    async def fake_select(*a, **kw):
        called["v2"] = True
        return ["should not happen"]

    monkeypatch.setattr(turn.v2_selection, "select", fake_select)

    snapshot = _snapshot({"cond.bernoulli": 0.3}, gray=frozenset({"cond.bernoulli"}))
    hints = await turn.run_clarification_detection(**_kwargs(snapshot=snapshot))

    assert called["v2"] is False
    assert hints and "direction" in hints[0].lower()


async def test_ranker_on_but_resolver_v2_off_falls_back_to_v1_and_logs(monkeypatch, caplog):
    """§8.2 row 3: RANKER ON, RESOLVER_V2 OFF, ENABLED ON -> no snapshot ever
    produced in real life; even if one is passed, v2 must not run."""
    _patch_write(monkeypatch)
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")
    monkeypatch.delenv("APOLLO_RESOLVER_V2", raising=False)

    called = {"v2": False}

    async def fake_select(*a, **kw):
        called["v2"] = True
        return ["should not happen"]

    monkeypatch.setattr(turn.v2_selection, "select", fake_select)

    import logging

    caplog.set_level(logging.INFO, logger="apollo.clarification.turn")

    hints = await turn.run_clarification_detection(**_kwargs(snapshot=None))

    assert called["v2"] is False
    assert hints and "direction" in hints[0].lower()
    assert "clarification_v2_no_resolver_v2" in caplog.text


async def test_all_three_flags_on_uses_v2_selection(monkeypatch):
    """§8.2 row 4: new behavior -- v2_selection.select drives the result."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_RESOLVER_V2", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")

    async def fake_select(snapshot, candidates, db, attempt_id, **kw):
        return ["v2 hint"]

    monkeypatch.setattr(turn.v2_selection, "select", fake_select)

    snapshot = _snapshot({"cond.bernoulli": 0.3}, gray=frozenset({"cond.bernoulli"}))
    hints = await turn.run_clarification_detection(**_kwargs(snapshot=snapshot))

    assert hints == ["v2 hint"]


async def test_v2_exception_falls_back_to_v1_and_logs_warning(monkeypatch, caplog):
    """§8.3 fail-open: an exception inside v2_selection.select must not
    propagate -- the v1 result is returned instead, with a warning logged."""
    _patch_write(monkeypatch)
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_RESOLVER_V2", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")

    async def boom(*a, **kw):
        raise RuntimeError("v2 blew up")

    monkeypatch.setattr(turn.v2_selection, "select", boom)

    import logging

    caplog.set_level(logging.WARNING, logger="apollo.clarification.turn")

    snapshot = _snapshot({"cond.bernoulli": 0.3}, gray=frozenset({"cond.bernoulli"}))
    hints = await turn.run_clarification_detection(**_kwargs(snapshot=snapshot))

    assert hints and "direction" in hints[0].lower()
    assert "clarification_v2_ranker_failed_falling_back_to_v1" in caplog.text
    assert "RuntimeError" in caplog.text


async def test_missing_snapshot_falls_back_to_v1_even_with_all_flags_on(monkeypatch):
    """§8.3: missing (None) snapshot -> v1, regardless of flags."""
    _patch_write(monkeypatch)
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_RESOLVER_V2", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")

    called = {"v2": False}

    async def fake_select(*a, **kw):
        called["v2"] = True
        return ["should not happen"]

    monkeypatch.setattr(turn.v2_selection, "select", fake_select)

    hints = await turn.run_clarification_detection(**_kwargs(snapshot=None))

    assert called["v2"] is False
    assert hints and "direction" in hints[0].lower()


async def test_v2_empty_pool_returns_no_probes(monkeypatch):
    """Empty V2 pool is a valid outcome (no probes), not a fallback trigger."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_RANKER", "true")
    monkeypatch.setenv("APOLLO_RESOLVER_V2", "true")
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "true")

    async def fake_select(*a, **kw):
        return []

    monkeypatch.setattr(turn.v2_selection, "select", fake_select)

    snapshot = _snapshot({}, gray=frozenset())
    hints = await turn.run_clarification_detection(**_kwargs(snapshot=snapshot))

    assert hints == []


async def test_existing_v1_callers_without_new_kwargs_unchanged(monkeypatch):
    """No new kwargs supplied at all -- signature stays backward compatible."""
    _patch_write(monkeypatch)
    hints = await turn.run_clarification_detection(**_kwargs())
    assert hints and "direction" in hints[0].lower()
