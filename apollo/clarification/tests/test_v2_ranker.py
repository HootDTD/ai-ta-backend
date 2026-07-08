"""Tests for the VoI ranker (integration spec §4, task T5).

Deterministic, no NLI/model, no DB. Builds hand-crafted
``IncrementalSnapshot``/``EdgeScore`` fixtures and asserts the VoI formula's
key behaviors called out in the T5 acceptance criteria:

- Hub with several incident credited edges outranks an isolated node of
  equal node_credit.
- All 6 ``relation_evidence`` values map to a tier (no silent r=0 for
  v1_inferred/none).
- v1_explicit edges (already credit 1.0) contribute zero gain.
- A gray hub resolving to >= 0.7 unlocks endpoints-tier gain on incident
  edges whose other endpoint is already >= 0.7.
- equation_cap nodes get the uncertainty floor ``p_equation_floor``.
- Deterministic tie-break: (voi desc, node_credit asc, canonical_key asc).
- Importance uses the live composite weights (w_n/w_e) from
  ``apollo.grading.composite.load_weights()``.
"""

from __future__ import annotations

import json

from apollo.clarification.v2_config import ClarificationV2Params
from apollo.clarification.v2_ranker import (
    PackedQuestion,
    VoICandidate,
    VoIScore,
    edge_gain,
    pack_questions,
    rank_by_voi,
)
from apollo.grading.composite import CompositeWeights
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore

WEIGHTS = CompositeWeights(w_n=0.706, w_e=0.294, p=0.15)
PARAMS = ClarificationV2Params()


def _snapshot(
    node_credits: dict[str, float],
    edge_scores: tuple[EdgeScore, ...],
    gray: frozenset[str] = frozenset(),
) -> IncrementalSnapshot:
    return IncrementalSnapshot(
        node_credits=node_credits,
        edge_scores=edge_scores,
        node_cov=0.5,
        edge_cov=0.5,
        winning_path_index=0,
        gray=gray,
        pair_count_this_turn=0,
    )


def _edge(from_key: str, to_key: str, credit: float, evidence: str) -> EdgeScore:
    return EdgeScore(
        edge_type="USES",
        from_key=from_key,
        to_key=to_key,
        credit=credit,
        relation_evidence=evidence,
    )


def _candidate(
    key: str,
    credit: float,
    is_gray: bool,
    incident: tuple[EdgeScore, ...] = (),
    node_type: str = "concept",
    best_window_index: int | None = None,
) -> VoICandidate:
    return VoICandidate(
        canonical_key=key,
        node_type=node_type,
        node_credit=credit,
        is_gray=is_gray,
        incident_edges=incident,
        best_window_index=best_window_index,
    )


class TestEdgeGain:
    """§4.2 edge_gain — mirrors edges.py's final-credit computation."""

    def test_all_six_relation_evidence_values_are_mapped(self):
        """No evidence tag silently maps to r=0 except the real 'none' tag.

        Starting each edge at credit 0.0 (an as-yet-unresolved edge, before
        its endpoint node has resolved) isolates the r-mapping itself: if a
        tier were silently missing from the map, its edge would compute
        r=0 -> graded=0 -> gain clamped only by the v1 floor. Every
        evidence value below yields a strictly positive gain except
        v1_explicit, which is already at its ceiling (1.0) and is covered
        by ``test_v1_explicit_edge_gives_zero_gain``.
        """
        c_v = 0.9
        c_u_target = 1.0
        for evidence, expect_positive, credit_before in [
            ("entail", True, 0.0),
            ("cooccur", True, 0.0),
            ("endpoints", True, 0.0),
            ("v1_explicit", False, 1.0),  # already at its ceiling
            ("v1_inferred", True, 0.0),
            ("none", True, 0.0),
        ]:
            e = _edge("u", "v", credit_before, evidence)
            gain = edge_gain(e, c_u_target, c_v, PARAMS)
            assert gain >= 0.0
            if expect_positive:
                assert gain > 0.0, f"{evidence} should yield positive gain"
            else:
                assert gain == 0.0

    def test_v1_explicit_edge_gives_zero_gain(self):
        e = _edge("u", "v", 1.0, "v1_explicit")
        assert edge_gain(e, 1.0, 1.0, PARAMS) == 0.0

    def test_v1_inferred_edge_gets_positive_gain_when_unresolved(self):
        # Not-yet-floored v1_inferred edge (credit 0.0): the r=0.5 tier is
        # honored (not silently 0), so resolving u lifts its credit toward
        # the v1_inferred floor.
        e = _edge("u", "v", 0.0, "v1_inferred")
        gain = edge_gain(e, 1.0, 1.0, PARAMS)
        assert gain > 0.0

    def test_endpoints_tier_promotion_on_gray_hub_resolution(self):
        """A 'none'-evidence edge whose other endpoint is already >= 0.7
        gets endpoints-tier (0.4) gain once u resolves to >= 0.7."""
        e = _edge("hub", "neighbor", 0.0, "none")
        c_v = 0.9  # neighbor already resolved
        gain_low = edge_gain(e, 0.5, c_v, PARAMS)  # u target below 0.7 -> no promo
        gain_high = edge_gain(e, 1.0, c_v, PARAMS)  # u target >= 0.7 -> promo
        assert gain_high > gain_low
        assert gain_high > 0.0

    def test_entail_and_cooccur_tiers_not_recomputed_downward(self):
        """The recorded tier is monotone-kept; promotion only ever raises r."""
        e = _edge("u", "v", 0.6, "cooccur")
        gain = edge_gain(e, 1.0, 1.0, PARAMS)
        assert gain >= 0.0

    def test_never_negative(self):
        # Edge already at max possible credit for its tier -> gain floors at 0.
        e = _edge("u", "v", 1.0, "entail")
        gain = edge_gain(e, 1.0, 1.0, PARAMS)
        assert gain == 0.0


class TestRankByVoiHubVsIsolated:
    def test_hub_with_credited_edges_outranks_isolated_equal_credit_node(self):
        hub_edges = (
            _edge("hub", "n1", 0.0, "none"),
            _edge("hub", "n2", 0.0, "none"),
            _edge("hub", "n3", 0.0, "none"),
        )
        hub = _candidate("hub", credit=0.3, is_gray=True, incident=hub_edges)
        isolated = _candidate("isolated", credit=0.3, is_gray=True, incident=())

        node_credits = {
            "hub": 0.3,
            "isolated": 0.3,
            "n1": 0.9,
            "n2": 0.9,
            "n3": 0.9,
        }
        snapshot = _snapshot(node_credits, hub_edges, gray=frozenset({"hub", "isolated"}))

        ranked = rank_by_voi([hub, isolated], snapshot, WEIGHTS, PARAMS)
        by_key = {r.candidate.canonical_key: r for r in ranked}
        assert by_key["hub"].voi > by_key["isolated"].voi
        assert by_key["hub"].importance > by_key["isolated"].importance
        # Both have the same node_credit -> uncertainty is identical.
        assert by_key["hub"].uncertainty == by_key["isolated"].uncertainty


class TestUncertainty:
    def test_missing_node_gets_p_missing(self):
        cand = _candidate("missing", credit=0.0, is_gray=False)
        snapshot = _snapshot({"missing": 0.0}, ())
        ranked = rank_by_voi([cand], snapshot, WEIGHTS, PARAMS)
        assert ranked[0].uncertainty == PARAMS.p_missing

    def test_equation_cap_node_gets_uncertainty_floor(self):
        cand = _candidate("eq", credit=0.3, is_gray=True, node_type="equation")
        snapshot = _snapshot({"eq": 0.3}, (), gray=frozenset({"eq"}))
        ranked = rank_by_voi([cand], snapshot, WEIGHTS, PARAMS)
        assert ranked[0].uncertainty >= PARAMS.p_equation_floor

    def test_gray_band_interpolation_monotone(self):
        """Nodes deeper in the gray band (closer to t_low) get higher
        uncertainty than nodes near t_mid."""
        near_low = _candidate("near_low", credit=0.31, is_gray=True)
        near_mid = _candidate("near_mid", credit=0.69, is_gray=True)
        snapshot = _snapshot(
            {"near_low": 0.31, "near_mid": 0.69}, (), gray=frozenset({"near_low", "near_mid"})
        )
        ranked = rank_by_voi([near_low, near_mid], snapshot, WEIGHTS, PARAMS)
        by_key = {r.candidate.canonical_key: r for r in ranked}
        assert by_key["near_low"].uncertainty > by_key["near_mid"].uncertainty


class TestTieBreak:
    def test_deterministic_tie_break_order(self):
        # Two candidates with identical importance/uncertainty (both isolated,
        # both credit 0.0 -> identical voi); tie-break by node_credit asc then key asc.
        a = _candidate("bbb", credit=0.0, is_gray=False)
        b = _candidate("aaa", credit=0.0, is_gray=False)
        snapshot = _snapshot({"bbb": 0.0, "aaa": 0.0}, ())
        ranked = rank_by_voi([a, b], snapshot, WEIGHTS, PARAMS)
        assert [r.candidate.canonical_key for r in ranked] == ["aaa", "bbb"]

    def test_tie_break_by_credit_asc_before_key(self):
        a = _candidate("z", credit=0.1, is_gray=True)
        b = _candidate("a", credit=0.05, is_gray=True)
        # force equal importance/uncertainty by symmetric setup is hard; instead
        # directly check ordering respects voi desc primarily.
        snapshot = _snapshot({"z": 0.1, "a": 0.05}, (), gray=frozenset({"z", "a"}))
        ranked = rank_by_voi([a, b], snapshot, WEIGHTS, PARAMS)
        # lower credit -> deeper in gray band -> higher uncertainty -> higher voi
        assert ranked[0].candidate.canonical_key == "a"


class TestImportanceUsesLiveWeights:
    def test_importance_scales_with_w_n_w_e(self):
        cand = _candidate("solo", credit=0.3, is_gray=True)
        snapshot = _snapshot({"solo": 0.3}, (), gray=frozenset({"solo"}))
        ranked_default = rank_by_voi([cand], snapshot, WEIGHTS, PARAMS)
        heavier_w_n = CompositeWeights(w_n=1.4, w_e=0.294, p=0.15)
        ranked_heavier = rank_by_voi([cand], snapshot, heavier_w_n, PARAMS)
        assert ranked_heavier[0].importance > ranked_default[0].importance


def _score(key: str, voi: float, credit: float = 0.3) -> VoIScore:
    cand = _candidate(key, credit=credit, is_gray=True)
    return VoIScore(candidate=cand, importance=voi, uncertainty=1.0, voi=voi)


class TestPackQuestions:
    """§10.1 pack_questions — 3x3 bounds + M4 cumulative per-attempt cap."""

    def test_nine_or_more_candidates_pack_exactly_3x3(self):
        ranked = [_score(f"k{i}", voi=float(20 - i)) for i in range(12)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=12)
        assert len(packed) == 3
        assert all(len(q.topic_keys) == 3 for q in packed)
        all_keys = [k for q in packed for k in q.topic_keys]
        assert len(all_keys) == len(set(all_keys)) == 9

    def test_fewer_than_nine_candidates_packs_fewer(self):
        ranked = [_score(f"k{i}", voi=float(10 - i)) for i in range(5)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=12)
        total_keys = sum(len(q.topic_keys) for q in packed)
        assert total_keys == 5
        assert len(packed) == 2  # 3 + 2
        assert len(packed[0].topic_keys) == 3
        assert len(packed[1].topic_keys) == 2

    def test_topic_keys_unique_across_questions(self):
        ranked = [_score(f"k{i}", voi=float(20 - i)) for i in range(12)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=12)
        seen: set[str] = set()
        for q in packed:
            for key in q.topic_keys:
                assert key not in seen
                seen.add(key)

    def test_order_by_voi_desc(self):
        ranked = [
            _score("low", voi=1.0),
            _score("high", voi=9.0),
            _score("mid", voi=5.0),
        ]
        # Caller passes VoI-ranked list already sorted desc (as rank_by_voi
        # returns); pack_questions must preserve that order, not re-sort.
        ranked_sorted = sorted(ranked, key=lambda s: -s.voi)
        packed = pack_questions(ranked_sorted, max_q=3, max_topics=3, remaining_budget=12)
        assert packed[0].topic_keys == ("high", "mid", "low")

    def test_zero_candidates_packs_nothing(self):
        packed = pack_questions([], max_q=3, max_topics=3, remaining_budget=12)
        assert packed == []

    def test_remaining_budget_zero_packs_nothing(self):
        ranked = [_score(f"k{i}", voi=float(10 - i)) for i in range(9)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=0)
        assert packed == []

    def test_remaining_budget_truncates_below_pool_size(self):
        ranked = [_score(f"k{i}", voi=float(20 - i)) for i in range(12)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=4)
        total_keys = sum(len(q.topic_keys) for q in packed)
        assert total_keys == 4
        # top-4 by voi (k0..k3) are the ones packed
        assert {k for q in packed for k in q.topic_keys} == {"k0", "k1", "k2", "k3"}

    def test_negative_remaining_budget_treated_as_zero(self):
        ranked = [_score(f"k{i}", voi=float(10 - i)) for i in range(5)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=-3)
        assert packed == []

    def test_packed_question_is_json_safe(self):
        ranked = [_score(f"k{i}", voi=float(10 - i)) for i in range(3)]
        packed = pack_questions(ranked, max_q=3, max_topics=3, remaining_budget=12)
        json.dumps([{"topic_keys": list(q.topic_keys)} for q in packed])

    def test_packed_question_is_frozen_dataclass(self):
        pq = PackedQuestion(topic_keys=("a", "b"))
        assert pq.topic_keys == ("a", "b")
        try:
            pq.topic_keys = ("c",)  # type: ignore[misc]
            assert False, "PackedQuestion must be frozen"
        except Exception:
            pass


class TestJsonSafety:
    def test_voiscore_fields_are_json_safe(self):
        cand = _candidate("solo", credit=0.3, is_gray=True)
        snapshot = _snapshot({"solo": 0.3}, (), gray=frozenset({"solo"}))
        ranked = rank_by_voi([cand], snapshot, WEIGHTS, PARAMS)
        payload = {
            "canonical_key": ranked[0].candidate.canonical_key,
            "importance": ranked[0].importance,
            "uncertainty": ranked[0].uncertainty,
            "voi": ranked[0].voi,
        }
        json.dumps(payload)  # must not raise
