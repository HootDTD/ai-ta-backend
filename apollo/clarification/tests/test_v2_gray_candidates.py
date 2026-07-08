"""Tests for candidate extraction (integration spec §2.1/§12, task T6).

``v2_gray_candidates(snapshot, params)`` builds the VoI ranking pool straight
from an ``IncrementalSnapshot`` — the design's §3.1 pseudocode line
``pool = v2_gray_candidates(snapshot)``. ``IncrementalSnapshot`` (T2) carries
only ``node_credits`` (float per key, for EVERY reference node — including
unscored ones at 0.0), ``edge_scores`` and the already-filtered ``gray``
frozenset (populated by ``incremental.score_turn``'s step 4b using exactly
the source-in-{nli,lexical_skip,equation_cap}-AND-is_gray predicate, spec
§5.3/§6) — it does not carry per-node ``node_type``/``source``/``best``
(``NodeScore`` fields), so this module cannot reconstruct them; those fields
default to the smallest safe placeholder (``node_type=""``,
``best_window_index=None``) on candidates built here.

Pool = ``snapshot.gray`` (already the gray/weak reference-node set) UNION
missing nodes (``node_credits[key] == 0.0``), MINUS any node at/above
``t_high`` (defensive — snapshot construction should never put a t_high node
in ``gray``, but this is asserted directly per the T6 acceptance criteria)
and MINUS misconception keys (``apollo.graph_compare.soundness
.is_misconception_key`` — the ``misc.*`` canonical-key prefix).
"""

from __future__ import annotations

from apollo.clarification.v2_config import ClarificationV2Params
from apollo.clarification.v2_ranker import VoICandidate, v2_gray_candidates
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore

PARAMS = ClarificationV2Params()


def _edge(from_key: str, to_key: str, credit: float = 0.0, evidence: str = "none") -> EdgeScore:
    return EdgeScore(
        edge_type="USES",
        from_key=from_key,
        to_key=to_key,
        credit=credit,
        relation_evidence=evidence,
    )


def _snapshot(
    node_credits: dict[str, float],
    edge_scores: tuple[EdgeScore, ...] = (),
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


class TestPoolMembership:
    def test_gray_node_included(self):
        snapshot = _snapshot({"gray1": 0.5}, gray=frozenset({"gray1"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        keys = {c.canonical_key for c in pool}
        assert keys == {"gray1"}

    def test_missing_zero_credit_node_included_even_if_not_in_gray(self):
        snapshot = _snapshot({"missing1": 0.0})
        pool = v2_gray_candidates(snapshot, PARAMS)
        keys = {c.canonical_key for c in pool}
        assert keys == {"missing1"}

    def test_resolved_node_excluded(self):
        """Neither gray nor zero-credit -> not in the pool."""
        snapshot = _snapshot({"resolved1": 1.0})
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool == []

    def test_t_high_node_excluded_even_if_flagged_gray(self):
        """Defensive exclusion: a node at/above t_high must never surface,
        even if (malformed input) it is present in ``snapshot.gray``."""
        snapshot = _snapshot({"hi": 0.95}, gray=frozenset({"hi"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool == []

    def test_misconception_node_excluded_from_gray(self):
        snapshot = _snapshot({"misc.overgeneralized": 0.5}, gray=frozenset({"misc.overgeneralized"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool == []

    def test_misconception_node_excluded_even_when_missing(self):
        snapshot = _snapshot({"misc.wrong_sign": 0.0})
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool == []

    def test_mixed_pool_correctly_partitioned(self):
        snapshot = _snapshot(
            {
                "gray1": 0.5,
                "missing1": 0.0,
                "resolved1": 1.0,
                "hi_flagged_gray": 0.95,
                "misc.bad": 0.4,
            },
            gray=frozenset({"gray1", "hi_flagged_gray", "misc.bad"}),
        )
        pool = v2_gray_candidates(snapshot, PARAMS)
        keys = {c.canonical_key for c in pool}
        assert keys == {"gray1", "missing1"}


class TestCandidateFields:
    def test_is_gray_flag_reflects_snapshot_gray_set(self):
        snapshot = _snapshot(
            {"gray1": 0.5, "missing1": 0.0}, gray=frozenset({"gray1"})
        )
        pool = v2_gray_candidates(snapshot, PARAMS)
        by_key = {c.canonical_key: c for c in pool}
        assert by_key["gray1"].is_gray is True
        assert by_key["missing1"].is_gray is False

    def test_node_credit_matches_snapshot(self):
        snapshot = _snapshot({"gray1": 0.42}, gray=frozenset({"gray1"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool[0].node_credit == 0.42

    def test_incident_edges_collected_for_both_endpoints(self):
        edges = (
            _edge("hub", "n1"),
            _edge("n2", "hub"),
            _edge("n3", "n4"),  # unrelated to "hub"
        )
        snapshot = _snapshot(
            {"hub": 0.5, "n1": 0.9, "n2": 0.9, "n3": 0.9, "n4": 0.9},
            edge_scores=edges,
            gray=frozenset({"hub"}),
        )
        pool = v2_gray_candidates(snapshot, PARAMS)
        hub_candidate = next(c for c in pool if c.canonical_key == "hub")
        assert set(hub_candidate.incident_edges) == {edges[0], edges[1]}

    def test_candidate_with_no_incident_edges_gets_empty_tuple(self):
        snapshot = _snapshot({"isolated": 0.3}, gray=frozenset({"isolated"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool[0].incident_edges == ()

    def test_best_window_index_defaults_to_none(self):
        """IncrementalSnapshot carries no per-node ``NodeScore.best`` --
        this module cannot recover it, so candidates built here always get
        ``best_window_index=None``."""
        snapshot = _snapshot({"gray1": 0.5}, gray=frozenset({"gray1"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert pool[0].best_window_index is None

    def test_deterministic_key_ordering(self):
        snapshot = _snapshot(
            {"zzz": 0.0, "aaa": 0.5, "mmm": 0.0}, gray=frozenset({"aaa"})
        )
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert [c.canonical_key for c in pool] == ["aaa", "mmm", "zzz"]

    def test_returns_voi_candidate_instances(self):
        snapshot = _snapshot({"gray1": 0.5}, gray=frozenset({"gray1"}))
        pool = v2_gray_candidates(snapshot, PARAMS)
        assert all(isinstance(c, VoICandidate) for c in pool)
