"""Tests for T4: the seeding path in the incremental scorer (design §6).

Acceptance criteria under test:

* ``seed(state, keys)`` returns a NEW ``IncrementalState`` (immutable) with
  the given keys pinned at running credit 1.0 and added to ``seeded_keys``.
* A seeded key is excluded from ``score_turn``'s candidate pool -- zero NLI
  pairs are ever spent on it, regardless of how it was scored before seeding.
* A seeded node stays frozen at full credit across subsequent turns.
* Its incident edges' credit CAN rise (recomputed from running node
  credits), but seeding must NEVER, by itself, pull the OTHER endpoint up to
  ``edge_pullup_floor`` -- that pull-up only fires from real
  ``relation_evidence == "entail"`` evidence earned this attempt (M5).
"""

from __future__ import annotations

import pytest

from apollo.graph_compare.canonical import CanonicalEdge, CanonicalNode, ReferenceGraph, ReferencePathView
from apollo.ontology.edges import EdgeType
from apollo.resolution.nli_adjudicator import NLIResult
from apollo.resolver_v2 import incremental
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.incremental_types import IncrementalState
from apollo.resolver_v2.types import RefNode

_PARAMS = ResolverV2Params()

_TURN_0 = "The pipe narrows and water speeds up."
_TURN_1 = "Mass is conserved in the pipe."

_LABEL_A = "concept A label"
_LABEL_B = "concept B label"


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


def _reference() -> ReferenceGraph:
    return ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="concept.a",
                node_type="definition",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
            CanonicalNode(
                canonical_key="concept.b",
                node_type="definition",
                source_node_ids=("r2",),
                evidence_spans=(),
            ),
        ),
        edges=(
            CanonicalEdge(
                edge_type=EdgeType.USES,
                from_key="concept.a",
                to_key="concept.b",
                provenance="explicit",
            ),
        ),
        paths=(ReferencePathView(canonical_keys=("concept.a", "concept.b")),),
    )


def _ref_nodes() -> tuple[RefNode, ...]:
    return (
        RefNode(canonical_key="concept.a", node_type="definition", label=_LABEL_A, views=(_LABEL_A,)),
        RefNode(canonical_key="concept.b", node_type="definition", label=_LABEL_B, views=(_LABEL_B,)),
    )


def _nli(entailment: float, contradiction: float = 0.0) -> NLIResult:
    neutral = max(0.0, 1.0 - entailment - contradiction)
    label = max(
        ("entailment", entailment),
        ("neutral", neutral),
        ("contradiction", contradiction),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, entailment, contradiction, neutral, "fake-nli")


class RecordingFakeNLI:
    """Stub adjudicator: low-signal default, exact-pair overrides, records
    every (premise, hypothesis) pair it was asked to classify."""

    def __init__(self, overrides: dict[tuple[str, str], NLIResult] | None = None):
        self._overrides = overrides or {}
        self.calls = 0
        self.seen_hypotheses: list[str] = []

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.calls += 1
        self.seen_hypotheses.append(hypothesis)
        return self._overrides.get((premise, hypothesis), _nli(0.05))


def _lexical_select():
    from apollo.resolver_v2.prefilter import select_windows

    return select_windows


# ---------------------------------------------------------------------------
# 1. seed() is immutable and pins credit/source/seeded_keys
# ---------------------------------------------------------------------------


def test_seed_returns_new_state_and_pins_fields():
    state = _empty_state()
    seeded = incremental.seed(state, ["concept.a"])

    assert seeded is not state
    # original untouched (immutability)
    assert state.running_node_max == {}
    assert state.seeded_keys == frozenset()

    assert seeded.running_node_max["concept.a"] == pytest.approx(1.0)
    assert seeded.node_source["concept.a"] == "clarification"
    assert seeded.seeded_keys == frozenset({"concept.a"})
    # unrelated fields carried through unchanged
    assert seeded.window_cursor == state.window_cursor
    assert seeded.pair_count_total == state.pair_count_total


def test_seed_multiple_keys_and_preserves_existing_seeded_keys():
    state = replace_seeded(_empty_state(), frozenset({"concept.x"}))
    seeded = incremental.seed(state, ["concept.a", "concept.b"])

    assert seeded.seeded_keys == frozenset({"concept.x", "concept.a", "concept.b"})
    assert seeded.running_node_max["concept.a"] == pytest.approx(1.0)
    assert seeded.running_node_max["concept.b"] == pytest.approx(1.0)


def replace_seeded(state: IncrementalState, seeded_keys: frozenset[str]) -> IncrementalState:
    from dataclasses import replace as _replace

    return _replace(state, seeded_keys=seeded_keys)


# ---------------------------------------------------------------------------
# 2. A seeded key spends zero NLI pairs and stays frozen at 1.0
# ---------------------------------------------------------------------------


def test_seeded_node_frozen_and_spends_zero_pairs():
    turns = (_TURN_0, _TURN_1)
    reference = _reference()
    ref_nodes = _ref_nodes()
    select_fn = _lexical_select()

    # If NOT seeded, this override would score concept.a highly -- proves the
    # seed exclusion, not merely an absence of matching text.
    overrides = {(_TURN_0, _LABEL_A): _nli(0.95)}
    nli = RecordingFakeNLI(overrides)

    state = incremental.seed(_empty_state(), ["concept.a"])
    state, snapshot = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )

    assert snapshot.node_credits["concept.a"] == pytest.approx(1.0)
    assert "concept.a" in state.seeded_keys
    # no NLI pair was ever issued with concept.a's label as hypothesis
    assert _LABEL_A not in nli.seen_hypotheses
    assert "concept.a" not in snapshot.gray


def test_seeded_node_stays_frozen_across_further_turns():
    turns = (_TURN_0,)
    reference = _reference()
    ref_nodes = _ref_nodes()
    select_fn = _lexical_select()
    nli = RecordingFakeNLI({})

    state = incremental.seed(_empty_state(), ["concept.a"])
    state, snapshot_1 = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )
    calls_before = nli.calls
    state, snapshot_2 = incremental.score_turn(
        state,
        all_student_turns=turns + (_TURN_1,),
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )

    assert snapshot_1.node_credits["concept.a"] == pytest.approx(1.0)
    assert snapshot_2.node_credits["concept.a"] == pytest.approx(1.0)
    # the only pairs spent this turn (if any) belong to concept.b, never a
    assert _LABEL_A not in nli.seen_hypotheses
    assert nli.calls >= calls_before  # sanity: counter is monotone, not decreasing


# ---------------------------------------------------------------------------
# 3. Incident edge credit rises once BOTH endpoints clear the threshold, but
#    seeding never pulls the OTHER endpoint up via edge_pullup_floor (M5)
# ---------------------------------------------------------------------------


def test_seed_lifts_incident_edge_once_other_endpoint_independently_resolves():
    turns = (_TURN_0,)
    reference = _reference()
    ref_nodes = _ref_nodes()
    select_fn = _lexical_select()
    nli = RecordingFakeNLI({})

    # concept.b resolves via the v1 floor (a REAL resolution path, not NLI),
    # independent of concept.a's seed.
    state = incremental.seed(_empty_state(), ["concept.a"])
    state, snapshot = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset({"concept.b"}),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )

    assert snapshot.node_credits["concept.a"] == pytest.approx(1.0)
    assert snapshot.node_credits["concept.b"] == pytest.approx(1.0)

    (edge,) = snapshot.edge_scores
    # Both endpoints cleared the ENDPOINTS-tier threshold (>=0.7) so the edge
    # credit rises via r*sqrt(c_a*c_b) -- WITHOUT any entail evidence.
    assert edge.relation_evidence == "endpoints"
    assert edge.credit == pytest.approx(0.4 * (1.0 * 1.0) ** 0.5)
    assert edge.credit > 0.0


def test_seed_does_not_pull_up_neighbor_absent_entail_evidence():
    turns = (_TURN_0,)
    reference = _reference()
    ref_nodes = _ref_nodes()
    select_fn = _lexical_select()
    # Nothing entails concept.b's label -- no real teaching evidence at all.
    nli = RecordingFakeNLI({})

    state = incremental.seed(_empty_state(), ["concept.a"])
    state, snapshot = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )

    assert snapshot.node_credits["concept.a"] == pytest.approx(1.0)  # frozen by seed
    # concept.b was NEVER taught and there is no entail evidence on the
    # incident edge -- seeding concept.a alone must not lift it to
    # edge_pullup_floor.
    assert snapshot.node_credits["concept.b"] < _PARAMS.edge_pullup_floor
    (edge,) = snapshot.edge_scores
    assert edge.relation_evidence != "entail"
