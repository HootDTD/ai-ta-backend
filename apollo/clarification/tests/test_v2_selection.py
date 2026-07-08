"""Tests for the V2 selection pipeline (integration spec §2.1/§3.1, task T9).

Thin-orchestrator test: pool -> dedup -> rank -> pack -> hints -> write,
end-to-end on a hand-built ``IncrementalSnapshot`` + closed candidate set,
with ``store.load_asked_candidate_keys``/``write_asked_waiting`` monkeypatched
(the same pattern ``test_turn.py`` uses for the v1 path) -- no real DB/network.
"""

from __future__ import annotations

from apollo.clarification import v2_selection
from apollo.resolution.candidates import Candidate
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore


def _candidate(key: str, node_type: str, display: str) -> Candidate:
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


def _edge(from_key: str, to_key: str, credit: float = 0.0, evidence: str = "none") -> EdgeScore:
    return EdgeScore(
        edge_type="USES", from_key=from_key, to_key=to_key, credit=credit, relation_evidence=evidence
    )


def _snapshot(node_credits: dict[str, float], gray: frozenset[str]) -> IncrementalSnapshot:
    return IncrementalSnapshot(
        node_credits=node_credits,
        edge_scores=(),
        node_cov=0.5,
        edge_cov=0.5,
        winning_path_index=0,
        gray=gray,
        pair_count_this_turn=0,
    )


def _patch_store(monkeypatch, asked_keys=frozenset(), writes=None):
    if writes is None:
        writes = []

    async def fake_load(db, *, attempt_id):
        return set(asked_keys)

    async def fake_write(db, **kw):
        writes.append(kw)

    monkeypatch.setattr(v2_selection, "load_asked_candidate_keys", fake_load)
    monkeypatch.setattr(v2_selection, "write_asked_waiting", fake_write)
    return writes


async def test_end_to_end_returns_answer_blind_hints_and_persists(monkeypatch):
    writes = _patch_store(monkeypatch)

    snapshot = _snapshot({"cond.bernoulli": 0.3}, gray=frozenset({"cond.bernoulli"}))
    candidates = (_candidate("cond.bernoulli", "condition", "pressure lower where faster"),)

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=3,
    )

    assert len(hints) == 1
    assert "direction" in hints[0].lower()
    # Answer-blind: the candidate's own text never leaks into the hint.
    assert "pressure" not in hints[0].lower()
    assert "bernoulli" not in hints[0].lower()

    assert len(writes) == 1
    assert writes[0]["candidate_key"] == "cond.bernoulli"
    assert writes[0]["attempt_id"] == 1
    assert writes[0]["asked_turn"] == 3


async def test_empty_pool_returns_no_probes(monkeypatch):
    _patch_store(monkeypatch)
    snapshot = _snapshot({"cond.a": 0.95}, gray=frozenset())  # nothing gray/missing
    candidates = (_candidate("cond.a", "condition", "d"),)

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
    )
    assert hints == []


async def test_already_asked_candidate_key_is_deduped(monkeypatch):
    writes = _patch_store(monkeypatch, asked_keys=frozenset({"cond.bernoulli"}))
    snapshot = _snapshot({"cond.bernoulli": 0.3}, gray=frozenset({"cond.bernoulli"}))
    candidates = (_candidate("cond.bernoulli", "condition", "d"),)

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
    )
    assert hints == []
    assert writes == []


async def test_per_attempt_cap_reached_returns_no_new_probes(monkeypatch):
    """M4: once max_questions_per_attempt topics have been asked this
    attempt, selection packs zero new probes even with a nonempty pool."""
    already_asked = frozenset(f"cond.k{i}" for i in range(12))  # default cap = 12
    writes = _patch_store(monkeypatch, asked_keys=already_asked)
    snapshot = _snapshot({"cond.new": 0.3}, gray=frozenset({"cond.new"}))
    candidates = (_candidate("cond.new", "condition", "d"),)

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
    )
    assert hints == []
    assert writes == []


async def test_ranks_multiple_topics_and_packs_them(monkeypatch):
    writes = _patch_store(monkeypatch)
    node_credits = {"cond.a": 0.3, "cond.b": 0.0, "eq.c": 0.3}
    snapshot = _snapshot(node_credits, gray=frozenset({"cond.a", "eq.c"}))
    candidates = (
        _candidate("cond.a", "condition", "a"),
        _candidate("cond.b", "condition", "b"),
        _candidate("eq.c", "equation", "c"),
    )

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=7,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=2,
    )

    assert len(hints) == 3
    assert len(writes) == 3
    written_keys = {w["candidate_key"] for w in writes}
    assert written_keys == {"cond.a", "cond.b", "eq.c"}


async def test_equation_node_uncertainty_floor_fires_via_backfilled_node_type(monkeypatch):
    """Fix-wave MAJOR: `v2_gray_candidates` cannot recover `node_type` from
    the snapshot alone (it always builds candidates with `node_type=""`),
    so `select` must backfill it from the closed candidate set BEFORE
    ranking -- otherwise the equation-node uncertainty floor
    (`p_equation_floor`, §4.2) never actually fires for a real equation-cap
    node in the wired pipeline (only in unit tests that construct
    `VoICandidate(node_type=...)` directly).

    credit=0.65 sits close enough to t_mid (0.70, default) that plain band
    uncertainty (~0.36) is well under the 0.7 equation floor -- a clear,
    non-degenerate distinguisher.
    """
    _patch_store(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        v2_selection,
        "emit_trace",
        lambda trace, *, attempt_id: captured.setdefault("trace", trace),
    )

    snapshot = _snapshot({"eq.a": 0.65}, gray=frozenset({"eq.a"}))
    candidates = (_candidate("eq.a", "equation", "F = ma"),)

    await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=3,
    )

    trace = captured["trace"]
    assert len(trace.ranked) == 1
    entry = trace.ranked[0]
    assert entry.canonical_key == "eq.a"
    assert entry.uncertainty == 0.7  # p_equation_floor, not the ~0.36 band value


async def test_candidate_missing_from_closed_set_is_skipped_defensively(monkeypatch):
    """A pooled canonical_key with no matching Candidate is skipped rather
    than crashing selection (defensive; §8.3 fail-open spirit)."""
    writes = _patch_store(monkeypatch)
    snapshot = _snapshot({"cond.orphan": 0.3}, gray=frozenset({"cond.orphan"}))
    candidates: tuple[Candidate, ...] = ()  # closed set does not know this key

    hints = await v2_selection.select(
        snapshot,
        candidates,
        db=object(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
    )
    assert hints == []
    assert writes == []
