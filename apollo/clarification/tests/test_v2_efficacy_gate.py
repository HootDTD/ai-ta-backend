"""Tests for the §11.3/§8.1 offline selection-quality + calibration gate
(task T16).

Deterministic, no network, no NLI/model calls, no DB -- a labeled
multi-turn transcript fixture (human/oracle "genuinely weak" labels) run
through :func:`apollo.clarification.v2_efficacy_gate.run_efficacy_gate`,
comparing the VoI ranker (T5) against the v1 ``rubric_weight_for`` + cosine
ranking it replaces (``pacing.py``) on precision@3 -- fraction of the top-3
ranked topics that are genuinely weak (spec's "questions spent on
genuinely-weak-nodes" expressed as a fraction of the fixed 3-question
budget).

This is a PRE-flag-ON gate, not a runtime dependency: nothing in
``turn.py``/``chat.py``/``v2_selection.py`` imports this module. It exists so
CI proves the ranker earns its keep before ``APOLLO_CLARIFICATION_V2_RANKER``
is ever set to true in any environment (spec §12 T16 acceptance criteria).

Replay-coverage caveat (spec §11): the replay corpus passes
``clarification_trace=[]`` and cannot exercise the clarification loop --
this fixture is deliberately NOT drawn from replay; it is a hand-built,
human-labeled multi-turn transcript. This test suite does not claim replay
coverage.
"""

from __future__ import annotations

from apollo.clarification.v2_config import ClarificationV2Params
from apollo.clarification.v2_efficacy_gate import (
    PINNED_CALIBRATION_BAND,
    CalibrationBand,
    LabeledNode,
    LabeledTurn,
    precision_at_k,
    record_calibration_band,
    run_efficacy_gate,
    v1_ranking,
    v2_ranking,
)
from apollo.grading.composite import CompositeWeights
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore

# Pinned validation context (§8.1 calibration pin): the EXACT params this
# comparison ran against. Injected explicitly into every call in this file
# so the test is deterministic regardless of ambient env vars or future
# ResolverV2Params/ClarificationV2Params default drift -- the whole point of
# the pin is that a drift must be *visible*, not silently absorbed by
# re-reading fresh live defaults in the test itself.
_RESOLVER_PARAMS = ResolverV2Params(
    t_low=PINNED_CALIBRATION_BAND.t_low,
    t_mid=PINNED_CALIBRATION_BAND.t_mid,
    t_high=PINNED_CALIBRATION_BAND.t_high,
)
_CLARIFICATION_PARAMS = ClarificationV2Params(
    voi_target_credit=PINNED_CALIBRATION_BAND.voi_target_credit,
    p_missing=PINNED_CALIBRATION_BAND.p_missing,
    p_near_resolved=PINNED_CALIBRATION_BAND.p_near_resolved,
    p_gray_min=PINNED_CALIBRATION_BAND.p_gray_min,
    p_gray_max=PINNED_CALIBRATION_BAND.p_gray_max,
    p_equation_floor=PINNED_CALIBRATION_BAND.p_equation_floor,
)
_WEIGHTS = CompositeWeights(w_n=0.706, w_e=0.294, p=0.15)


def _edge(from_key: str, to_key: str, credit: float, evidence: str) -> EdgeScore:
    return EdgeScore(
        edge_type="USES", from_key=from_key, to_key=to_key, credit=credit, relation_evidence=evidence
    )


def _fixture_turns() -> tuple[LabeledTurn, ...]:
    """A labeled 3-turn transcript. Each turn is a snapshot's worth of
    reference-node candidates with oracle "genuinely weak" labels. Designed
    to expose the two structural blind spots of v1's
    ``(rubric_weight_for(node_type), cosine)`` ranking that VoI (importance *
    uncertainty, credit/edge-structure aware) does not share:

    Turn 1 (hub effect): all candidates share one node_type, so v1 collapses
    entirely to cosine -- a signal uncorrelated with actual resolution
    state. The genuinely weak hub node has misleadingly low cosine; a
    well-resolved decoy has misleadingly high cosine.

    Turn 2 (rubric-weight domination): candidates span node types with very
    different axis weights (procedure_step=0.57 down to definition=0.0). A
    well-taught, mostly-resolved procedure_step node outranks a genuinely
    weak, unresolved definition node under v1 purely because of its axis
    weight -- regardless of how little clarification value remains.

    Turn 3 (missing-node underranking): a never-scored ("missing", credit
    0.0) genuinely weak node has low cosine, so v1 buries it beneath
    well-resolved decoys with high cosine; VoI's ``p_missing`` uncertainty
    floor surfaces it instead.
    """
    turn_1 = LabeledTurn(
        turn_id="turn_1_hub",
        nodes=(
            LabeledNode(
                canonical_key="weak_hub",
                node_type="condition",
                node_credit=0.35,
                cosine=0.10,
                is_gray=True,
                incident_edges=(
                    _edge("weak_hub", "peer_a", 0.2, "cooccur"),
                    _edge("weak_hub", "peer_b", 0.2, "cooccur"),
                    _edge("weak_hub", "peer_c", 0.2, "cooccur"),
                ),
                is_genuinely_weak=True,
            ),
            LabeledNode(
                canonical_key="near_resolved_showy",
                node_type="condition",
                node_credit=0.68,
                cosine=0.95,
                is_gray=True,
                is_genuinely_weak=False,
            ),
            LabeledNode(
                canonical_key="missing_isolated",
                node_type="condition",
                node_credit=0.0,
                cosine=0.50,
                is_gray=False,
                is_genuinely_weak=True,
            ),
            LabeledNode(
                canonical_key="decoy_midtie",
                node_type="condition",
                node_credit=0.5,
                cosine=0.80,
                is_gray=True,
                is_genuinely_weak=False,
            ),
        ),
        extra_node_credits={"peer_a": 0.4, "peer_b": 0.5, "peer_c": 0.3},
    )

    turn_2 = LabeledTurn(
        turn_id="turn_2_rubric_weight_domination",
        nodes=(
            LabeledNode(
                canonical_key="well_taught_procedure",
                node_type="procedure_step",
                node_credit=0.85,
                cosine=0.60,
                is_gray=False,
                is_genuinely_weak=False,
            ),
            LabeledNode(
                canonical_key="mid_justification",
                node_type="condition",
                node_credit=0.6,
                cosine=0.50,
                is_gray=True,
                is_genuinely_weak=False,
            ),
            LabeledNode(
                canonical_key="weak_simplification",
                node_type="simplification",
                node_credit=0.2,
                cosine=0.30,
                is_gray=True,
                is_genuinely_weak=True,
            ),
            LabeledNode(
                canonical_key="weak_definition",
                node_type="definition",
                node_credit=0.05,
                cosine=0.90,
                is_gray=True,
                is_genuinely_weak=True,
            ),
        ),
    )

    turn_3 = LabeledTurn(
        turn_id="turn_3_missing_node_underranking",
        nodes=(
            LabeledNode(
                canonical_key="resolved_showy2",
                node_type="condition",
                node_credit=0.9,
                cosine=0.99,
                is_gray=False,
                is_genuinely_weak=False,
            ),
            LabeledNode(
                canonical_key="another_decoy",
                node_type="condition",
                node_credit=0.55,
                cosine=0.85,
                is_gray=True,
                is_genuinely_weak=False,
            ),
            LabeledNode(
                canonical_key="long_neglected_missing",
                node_type="condition",
                node_credit=0.0,
                cosine=0.20,
                is_gray=False,
                is_genuinely_weak=True,
            ),
            LabeledNode(
                canonical_key="quietly_weak",
                node_type="condition",
                node_credit=0.4,
                cosine=0.15,
                is_gray=True,
                is_genuinely_weak=True,
            ),
        ),
    )

    return (turn_1, turn_2, turn_3)


def test_precision_at_k_counts_weak_hits_in_top_k():
    weak = frozenset({"a", "c"})
    assert precision_at_k(["a", "b", "c", "d"], weak, 3) == 2 / 3
    assert precision_at_k(["b", "d", "a"], weak, 3) == 1 / 3
    assert precision_at_k([], weak, 3) == 0.0
    assert precision_at_k(["a"], weak, 0) == 0.0


def test_v1_ranking_ties_on_rubric_weight_then_breaks_on_cosine():
    turns = _fixture_turns()
    turn_1 = turns[0]
    # All turn_1 nodes share node_type="condition" -> rubric_weight ties ->
    # v1 collapses to pure cosine-desc.
    assert v1_ranking(turn_1) == [
        "near_resolved_showy",  # cosine 0.95
        "decoy_midtie",  # cosine 0.80
        "missing_isolated",  # cosine 0.50
        "weak_hub",  # cosine 0.10
    ]


def test_v1_ranking_lets_rubric_weight_dominate_across_node_types():
    turns = _fixture_turns()
    turn_2 = turns[1]
    ranking = v1_ranking(turn_2)
    # procedure_step (0.57) always outranks definition (0.0) under v1,
    # regardless of either node's actual resolution state or cosine.
    assert ranking[0] == "well_taught_procedure"
    assert ranking.index("well_taught_procedure") < ranking.index("weak_definition")


def test_v2_ranking_prioritizes_the_genuinely_weak_hub_over_showy_decoys():
    turns = _fixture_turns()
    turn_1 = turns[0]
    node_credits = {n.canonical_key: n.node_credit for n in turn_1.nodes}
    node_credits.update(turn_1.extra_node_credits)
    snapshot = IncrementalSnapshot(
        node_credits=node_credits,
        edge_scores=turn_1.nodes[0].incident_edges,
        node_cov=0.0,
        edge_cov=0.0,
        winning_path_index=0,
        gray=frozenset(n.canonical_key for n in turn_1.nodes if n.is_gray),
        pair_count_this_turn=0,
    )
    ranking = v2_ranking(turn_1, snapshot, _WEIGHTS, _CLARIFICATION_PARAMS)
    assert ranking[0] == "weak_hub"
    assert ranking.index("weak_hub") < ranking.index("near_resolved_showy")


def test_efficacy_gate_voi_beats_v1_on_every_fixture_turn():
    result = run_efficacy_gate(
        _fixture_turns(),
        weights=_WEIGHTS,
        params=_CLARIFICATION_PARAMS,
        resolver_params=_RESOLVER_PARAMS,
    )
    assert len(result.per_turn) == 3
    for turn_result in result.per_turn:
        assert turn_result.voi_precision_at_k >= turn_result.v1_precision_at_k, turn_result


def test_efficacy_gate_passes_voi_ge_v1_and_records_calibration_band():
    result = run_efficacy_gate(
        _fixture_turns(),
        weights=_WEIGHTS,
        params=_CLARIFICATION_PARAMS,
        resolver_params=_RESOLVER_PARAMS,
    )
    # The gate itself: flag-ON in ANY environment is blocked unless this
    # holds (spec §11.3/§12 T16 acceptance criteria).
    assert result.passed is True
    assert result.voi_mean_precision_at_k >= result.v1_mean_precision_at_k
    # Concrete margin (not just >=) -- proves the fixture actually
    # discriminates the two rankings rather than trivially tying at 0.
    assert result.voi_mean_precision_at_k > result.v1_mean_precision_at_k
    assert result.calibration_band == PINNED_CALIBRATION_BAND


def test_efficacy_gate_is_deterministic_across_repeated_runs():
    turns = _fixture_turns()
    first = run_efficacy_gate(
        turns, weights=_WEIGHTS, params=_CLARIFICATION_PARAMS, resolver_params=_RESOLVER_PARAMS
    )
    second = run_efficacy_gate(
        turns, weights=_WEIGHTS, params=_CLARIFICATION_PARAMS, resolver_params=_RESOLVER_PARAMS
    )
    assert first == second


def test_record_calibration_band_reads_injected_params_verbatim():
    band = record_calibration_band(_RESOLVER_PARAMS, _CLARIFICATION_PARAMS)
    assert band == PINNED_CALIBRATION_BAND
    assert isinstance(band, CalibrationBand)


def test_pinned_calibration_band_matches_live_config_defaults():
    """§8.1 calibration pin: the T16 comparison ran against
    ``ResolverV2Params()``/``ClarificationV2Params()`` DEFAULTS as of the
    2026-07-07 build. If either module's class defaults are ever
    recalibrated, this assertion fails -- the signal that flag-ON is no
    longer valid against the pinned comparison and it must be re-run (spec
    §8.1: "flag-ON is only valid while the live load_params() band matches
    that recorded band, or the comparison is re-run")."""
    live_band = record_calibration_band(ResolverV2Params(), ClarificationV2Params())
    assert live_band == PINNED_CALIBRATION_BAND


def test_efficacy_gate_empty_transcript_does_not_crash_and_fails_open_to_passed():
    result = run_efficacy_gate(
        (), weights=_WEIGHTS, params=_CLARIFICATION_PARAMS, resolver_params=_RESOLVER_PARAMS
    )
    assert result.per_turn == ()
    assert result.v1_mean_precision_at_k == 0.0
    assert result.voi_mean_precision_at_k == 0.0
    # 0.0 >= 0.0 -- vacuously "passed" for an empty fixture; a real fixture
    # (as this file ships) is what the gate actually certifies.
    assert result.passed is True
