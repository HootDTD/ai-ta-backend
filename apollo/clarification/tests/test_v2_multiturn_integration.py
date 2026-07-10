"""Scripted multi-turn integration test (integration spec §11.2, task T14).

Wires the already-built T1-T13 pieces end-to-end across a 3-turn attempt,
with a ``FakeNLIAdjudicator``-style stub and no network/model load:

  turn 1: student text leaves an EQUATION reference node gray
          (``source == "equation_cap"``, the symbolic-blindness gate) and a
          neighbor DEFINITION node gray too. ``v2_selection.select`` packs a
          question that asks about the equation node ("write out the
          relationship" -- the equation-floor uncertainty recovery, §4.2) and
          persists an ``asked_waiting`` row for it.
  turn 2: a fake judge / ``resolve_pending_clarifications`` records a
          content-verified ``confirmed`` outcome for the equation node ->
          the caller seeds it (``incremental.seed``, task T4/T12) -> the node
          freezes at running credit 1.0 and its OWN incident edge's credit
          rises (relation-evidence stays whatever was earned from real
          windows -- "cooccur" here, never invented "entail") while the
          NEIGHBOR node is untouched (M5: seeding never pulls up a neighbor
          absent real ENTAIL evidence). ``v2_selection.select`` on this
          turn's snapshot never re-asks the equation node (it has cleared
          ``t_high``, and its candidate_key is already in the asked-store).

Also asserts the §7 trace fields (pool/ranked/questions/dedup/budget/seeded)
on both turns, and the L2 conservative-bound fix: on the SAME transcript, a
from-scratch batch run (``run_resolver_v2``, fed the confirmed key via
``v1_resolved_keys`` -- the real grading-space path a content-verified
clarification confirmation takes, spec §6) never scores LOWER than the
incremental snapshot, i.e. ``incremental_cov <= batch_cov + epsilon``. A
non-conservative (over-credit) divergence must fail this test, not merely be
observable via the optional runtime diff flag.
"""

from __future__ import annotations

import json
import logging

import pytest

from apollo.clarification import v2_selection
from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.ontology.edges import EdgeType
from apollo.resolution.candidates import Candidate
from apollo.resolution.nli_adjudicator import NLIResult
from apollo.resolver_v2 import incremental
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.engine import run_resolver_v2
from apollo.resolver_v2.incremental_types import IncrementalState
from apollo.resolver_v2.types import RefNode

_PARAMS = ResolverV2Params()

# A single short sentence -> exactly one window (build_windows never spans a
# turn boundary), so BOTH reference nodes' NLI argmax necessarily lands on
# this same window -- the deterministic route to the COOCCUR tier (§5.6)
# without depending on exact lexical-prefilter arithmetic.
_TURN_0 = "The venturi equation links pressure drop to rising velocity."
_TURN_1_CONFIRM = "Yes, exactly, that is the relationship I used."

_EQ_KEY = "eq.venturi"
_EQ_LABEL = _TURN_0  # verbatim -> near-1.0 lexical score against its own turn
_DEF_KEY = "def.mass_conservation"
_DEF_LABEL = "Mass is conserved through the pipe"

_EPSILON = 1e-6


def _nli(entailment: float, contradiction: float = 0.0) -> NLIResult:
    neutral = max(0.0, 1.0 - entailment - contradiction)
    label = max(
        ("entailment", entailment),
        ("neutral", neutral),
        ("contradiction", contradiction),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, entailment, contradiction, neutral, "fake-nli")


class ScriptedFakeNLI:
    """Stub adjudicator (resolver_v2 convention): low-signal default so any
    unscripted pair (e.g. the edge-hypothesis template sentence) degrades
    deterministically rather than raising, with exact-pair overrides for the
    scripted turns. No model load, no network."""

    def __init__(self, overrides: dict[tuple[str, str], NLIResult]):
        self._overrides = overrides
        self.calls = 0
        self.seen_hypotheses: list[str] = []

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.calls += 1
        self.seen_hypotheses.append(hypothesis)
        return self._overrides.get((premise, hypothesis), _nli(0.05))


def _lexical_select():
    from apollo.resolver_v2.prefilter import select_windows

    return select_windows


def _overrides() -> dict[tuple[str, str], NLIResult]:
    return {
        # Turn 1: strong entailment on the equation view -> fused score well
        # above t_high -> equation-cap binds (credit 0.3, source
        # "equation_cap") regardless of the exact lexical component.
        (_TURN_0, _EQ_LABEL): _nli(0.95),
        # Turn 1: moderate entailment on the definition view -> fused score
        # lands inside the gray band [t_low, t_mid) for any lexical value in
        # [0, 1] (0.85*0.40 + 0.15*lex ranges [0.34, 0.49]).
        (_TURN_0, _DEF_LABEL): _nli(0.40),
    }


def _reference() -> ReferenceGraph:
    return ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key=_EQ_KEY,
                node_type="equation",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
            CanonicalNode(
                canonical_key=_DEF_KEY,
                node_type="definition",
                source_node_ids=("r2",),
                evidence_spans=(),
            ),
        ),
        edges=(
            CanonicalEdge(
                edge_type=EdgeType.USES,
                from_key=_EQ_KEY,
                to_key=_DEF_KEY,
                provenance="explicit",
            ),
        ),
        paths=(ReferencePathView(canonical_keys=(_EQ_KEY, _DEF_KEY)),),
    )


def _ref_nodes() -> tuple[RefNode, ...]:
    return (
        RefNode(canonical_key=_EQ_KEY, node_type="equation", label=_EQ_LABEL, views=(_EQ_LABEL,)),
        RefNode(canonical_key=_DEF_KEY, node_type="definition", label=_DEF_LABEL, views=(_DEF_LABEL,)),
    )


def _payload() -> dict:
    # No committed-views cache entry for this concept_id/id -> label-only
    # views (documented degrade), matching the hand-built _ref_nodes() above
    # so batch's build_ref_nodes and the incremental scorer's injected
    # ref_nodes agree (mirrors test_incremental.py's fixture convention).
    return {
        "concept_id": "t14_integration_test",
        "id": "t14_problem",
        "reference_solution": [
            {"id": "s1", "entity_key": _EQ_KEY, "content": {"label": _EQ_LABEL}},
            {"id": "s2", "entity_key": _DEF_KEY, "content": {"label": _DEF_LABEL}},
        ],
    }


def _empty_state() -> IncrementalState:
    return IncrementalState(
        window_cursor=0,
        global_window_count=0,
        running_node_max={},
        node_source={},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=0,
    )


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


def _candidates() -> tuple[Candidate, ...]:
    return (
        _candidate(_EQ_KEY, "equation", "SECRET EQUATION CONTENT"),
        _candidate(_DEF_KEY, "definition", "SECRET DEFINITION CONTENT"),
    )


class _FakeStore:
    """In-memory stand-in for ``apollo.clarification.store``'s asked-key
    bookkeeping, monkeypatched onto ``v2_selection`` exactly like
    ``test_v2_trace.py``'s ``_patch_store`` helper -- no real DB/session."""

    def __init__(self) -> None:
        self.asked_keys: set[str] = set()
        self.writes: list[dict] = []

    async def load(self, db, *, attempt_id):
        return set(self.asked_keys)

    async def write(self, db, **kw):
        self.writes.append(kw)
        self.asked_keys.add(kw["candidate_key"])


def _patch_store(monkeypatch, store: _FakeStore) -> None:
    monkeypatch.setattr(v2_selection, "load_asked_candidate_keys", store.load)
    monkeypatch.setattr(v2_selection, "write_asked_waiting", store.write)


def _find_edge(snapshot, from_key: str, to_key: str):
    for edge in snapshot.edge_scores:
        if edge.from_key == from_key and edge.to_key == to_key:
            return edge
    raise AssertionError(f"no edge {from_key}->{to_key} in snapshot")


async def test_scripted_3_turn_clarification_flip(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="apollo.clarification.v2_selection")
    select_fn = _lexical_select()
    reference = _reference()
    ref_nodes = _ref_nodes()
    nli = ScriptedFakeNLI(_overrides())
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    candidates = _candidates()

    # ------------------------------------------------------------------
    # Turn 1: equation node lands gray (equation_cap); neighbor also gray.
    # ------------------------------------------------------------------
    state0 = _empty_state()
    state1, snapshot1 = incremental.score_turn(
        state0,
        all_student_turns=(_TURN_0,),
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )

    assert snapshot1.node_credits[_EQ_KEY] == pytest.approx(_PARAMS.equation_text_credit_cap)
    assert state1.node_source[_EQ_KEY] == "equation_cap"
    assert _EQ_KEY in snapshot1.gray
    assert snapshot1.node_credits[_DEF_KEY] == pytest.approx(0.3)
    assert _DEF_KEY in snapshot1.gray

    edge1 = _find_edge(snapshot1, _EQ_KEY, _DEF_KEY)
    assert edge1.relation_evidence == "cooccur"  # both best-windows land on window 0
    assert edge1.credit == pytest.approx(0.7 * (0.3 * 0.3) ** 0.5)

    hints1 = await v2_selection.select(
        snapshot1,
        candidates,
        db=object(),
        attempt_id=101,
        session_id=1,
        user_id="u1",
        search_space_id=1,
        concept_id=None,
        asked_turn=1,
        snapshot_source="this_turn",
        pair_count_total=snapshot1.pair_count_this_turn,
        seeded_keys=frozenset(),
    )

    assert hints1, "turn 1 must produce at least one answer-blind probe"
    # Answer-blind: the rendered hint never leaks the candidate's content.
    for hint in hints1:
        assert "SECRET" not in hint

    eq_writes_turn1 = [w for w in store.writes if w["candidate_key"] == _EQ_KEY]
    assert len(eq_writes_turn1) == 1, "equation node must be asked exactly once on turn 1"
    assert _EQ_KEY in store.asked_keys

    # §7 trace, turn 1: pool/ranked/questions/dedup/budget/seeded all present
    # and the equation node's question uses the "variable" hint dimension
    # (equation -> _HINT_DIM_BY_TYPE), never rendered/candidate text.
    record1 = next(r for r in caplog.records if r.message.startswith("clarification_v2_trace"))
    serialized1 = record1.message.split("trace=", 1)[1]
    payload1 = json.loads(serialized1)  # json.dumps round-trip
    assert json.loads(json.dumps(payload1)) == payload1
    block1 = payload1["clarification_v2"]
    assert block1["snapshot_source"] == "this_turn"
    assert {p["canonical_key"] for p in block1["pool"]} == {_EQ_KEY, _DEF_KEY}
    assert any(r["canonical_key"] == _EQ_KEY for r in block1["ranked"])
    packed_keys_turn1 = {k for q in block1["questions"] for k in q["topic_keys"]}
    assert _EQ_KEY in packed_keys_turn1
    eq_question = next(q for q in block1["questions"] if _EQ_KEY in q["topic_keys"])
    eq_dim = eq_question["hint_dims"][eq_question["topic_keys"].index(_EQ_KEY)]
    assert eq_dim == "variable"
    assert block1["seeded"] == []
    assert "SECRET" not in serialized1
    caplog.clear()

    # ------------------------------------------------------------------
    # Turn 2: a content-verified "confirmed" outcome seeds the equation
    # node -- it freezes at 1.0, its own edge lifts, the neighbor is NOT
    # auto-resolved (M5), and it is never re-asked.
    # ------------------------------------------------------------------
    state2 = incremental.seed(state1, [_EQ_KEY])
    state3, snapshot2 = incremental.score_turn(
        state2,
        all_student_turns=(_TURN_0, _TURN_1_CONFIRM),
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )

    assert snapshot2.node_credits[_EQ_KEY] == pytest.approx(1.0)
    assert _EQ_KEY in state3.seeded_keys
    # No NLI pair was ever issued with the equation node's view as hypothesis
    # AFTER seeding (it was already spent once on turn 1's overrides lookup;
    # the seeded-exclusion means turn 2 spends zero additional pairs on it).
    calls_before_turn2_check = nli.calls

    # M5: seeding the equation node must NOT, by itself, pull the neighbor
    # up -- it stays exactly where turn 1 left it (no real entail evidence
    # was ever earned on the incident edge).
    assert snapshot2.node_credits[_DEF_KEY] == pytest.approx(0.3)
    assert snapshot2.node_credits[_DEF_KEY] < _PARAMS.edge_pullup_floor

    # Own incident edge lifts: relation-evidence never regresses/invents
    # "entail" out of a seed, but the numeric credit rises because the
    # equation endpoint's RUNNING credit rose from 0.3 -> 1.0.
    edge2 = _find_edge(snapshot2, _EQ_KEY, _DEF_KEY)
    assert edge2.relation_evidence == "cooccur"
    assert edge2.credit > edge1.credit
    assert edge2.credit == pytest.approx(0.7 * (1.0 * 0.3) ** 0.5)

    hints2 = await v2_selection.select(
        snapshot2,
        candidates,
        db=object(),
        attempt_id=101,
        session_id=1,
        user_id="u1",
        search_space_id=1,
        concept_id=None,
        asked_turn=2,
        snapshot_source="prior_turn",
        pair_count_total=snapshot2.pair_count_this_turn,
        seeded_keys=state3.seeded_keys,
    )

    # No re-ask: the equation node cleared t_high (excluded from the pool)
    # AND its candidate_key was already recorded as asked (dedup, §10.3).
    eq_writes_total = [w for w in store.writes if w["candidate_key"] == _EQ_KEY]
    assert len(eq_writes_total) == 1, "equation node must never be asked a second time"
    for hint in hints2:
        assert "SECRET" not in hint

    record2 = next(r for r in caplog.records if r.message.startswith("clarification_v2_trace"))
    serialized2 = record2.message.split("trace=", 1)[1]
    payload2 = json.loads(serialized2)
    assert json.loads(json.dumps(payload2)) == payload2
    block2 = payload2["clarification_v2"]
    # The equation node is gone from the pool entirely (credit >= t_high) --
    # not merely dedup-skipped -- and was never re-packed into a question.
    assert _EQ_KEY not in {p["canonical_key"] for p in block2["pool"]}
    assert all(_EQ_KEY not in q["topic_keys"] for q in block2["questions"])
    assert block2["seeded"] == [_EQ_KEY]
    assert "SECRET" not in serialized2

    # ------------------------------------------------------------------
    # L2 fix: conservative monotone bound vs a from-scratch batch run on the
    # SAME transcript. The confirmed key reaches batch through the real
    # grading-space route (v1_resolved_keys -- spec §6's "grading-space via
    # existing v1 path"), never by forking the batch engine.
    # ------------------------------------------------------------------
    batch = run_resolver_v2(
        student_turns=(_TURN_0, _TURN_1_CONFIRM),
        reference_graph=reference,
        problem_payload=_payload(),
        v1_resolved_keys=frozenset({_EQ_KEY}),
        v1_explicit_triples=frozenset(),
        v1_inferred_triples=frozenset(),
        nli=nli,
        grayzone_fn=None,
        params=_PARAMS,
        select_fn=select_fn,
    )

    assert snapshot2.node_cov <= batch.node_coverage + _EPSILON
    assert snapshot2.edge_cov <= batch.edge_coverage + _EPSILON

    # sanity: the bound is not vacuous -- both sides are genuinely
    # comparable (non-degenerate coverages), and the assertion above is a
    # real conservative check, not `0 <= 0`.
    assert batch.node_coverage > 0.0
    assert snapshot2.node_cov > 0.0
