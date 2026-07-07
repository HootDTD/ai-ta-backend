"""T1 smoke tests: resolver_v2 flags, params env overlay, trace JSON-safety.

Smoke tier per the design card (happy path + one edge case per public
function); the 95% patch gate is explicitly deferred for this branch.
"""

import json

import pytest

import apollo.resolver_v2 as resolver_v2_pkg
from apollo.resolver_v2.config import (
    GRAYZONE_FLAG,
    RESOLVER_V2_FLAG,
    TRACE_DIR_ENV,
    ResolverV2Params,
    grayzone_enabled,
    load_params,
    resolver_v2_enabled,
)
from apollo.resolver_v2.types import (
    EdgeScore,
    NodeScore,
    PairScore,
    ResolverV2Result,
)

_PARAM_ENVS = (
    RESOLVER_V2_FLAG,
    GRAYZONE_FLAG,
    TRACE_DIR_ENV,
    "APOLLO_RESOLVER_V2_T_LOW",
    "APOLLO_RESOLVER_V2_T_MID",
    "APOLLO_RESOLVER_V2_T_HIGH",
    "APOLLO_RESOLVER_V2_ALPHA",
    "APOLLO_RESOLVER_V2_MAX_CONTRADICTION",
    "APOLLO_RESOLVER_V2_TOP_K",
    "APOLLO_RESOLVER_V2_LEX_FLOOR",
    "APOLLO_RESOLVER_V2_MAX_PAIRS",
    "APOLLO_RESOLVER_V2_T_EDGE",
    "APOLLO_RESOLVER_V2_EDGE_PULLUP_FLOOR",
    "APOLLO_RESOLVER_V2_WINDOW_SENTENCES",
    "APOLLO_RESOLVER_V2_WINDOW_OVERLAP_SENTENCES",
    "APOLLO_RESOLVER_V2_MAX_WINDOW_WORDS",
    "APOLLO_RESOLVER_V2_MAX_GRAYZONE_NODES",
    "APOLLO_RESOLVER_V2_GRAYZONE_CREDIT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a clean env so defaults are deterministic."""
    for name in _PARAM_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


# --- flags -------------------------------------------------------------------


def test_flags_default_off():
    """DEFAULT-OFF is the design invariant: unset env means byte-identical
    v1 pipeline (master flag) and deterministic gray default (grayzone)."""
    assert resolver_v2_enabled() is False
    assert grayzone_enabled() is False


def test_flag_truthy_values(monkeypatch):
    for value in ("1", "true", "YES", " yes "):
        monkeypatch.setenv(RESOLVER_V2_FLAG, value)
        assert resolver_v2_enabled() is True
        monkeypatch.setenv(GRAYZONE_FLAG, value)
        assert grayzone_enabled() is True


def test_flag_non_truthy_values_stay_off(monkeypatch):
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(RESOLVER_V2_FLAG, value)
        assert resolver_v2_enabled() is False


# --- load_params ---------------------------------------------------------------


def test_load_params_defaults():
    """Clean env -> the T8 FITTED defaults, verbatim (see
    campaign/out/resolver-v2/calibration-2026-07-07.json)."""
    params = load_params()
    assert params == ResolverV2Params()
    assert (params.t_low, params.t_mid, params.t_high) == (0.30, 0.70, 0.90)
    assert params.alpha == 0.75
    assert params.max_contradiction == 0.30
    assert params.top_k_windows == 3
    assert params.max_nli_pairs == 200
    assert params.t_edge == 0.85
    assert params.trace_dir is None


def test_load_params_env_overrides(monkeypatch):
    """§7 env names win over defaults (note TOP_K/MAX_PAIRS map to the
    top_k_windows/max_nli_pairs fields)."""
    monkeypatch.setenv("APOLLO_RESOLVER_V2_T_LOW", "0.30")
    monkeypatch.setenv("APOLLO_RESOLVER_V2_ALPHA", "1.0")
    monkeypatch.setenv("APOLLO_RESOLVER_V2_TOP_K", "2")
    monkeypatch.setenv("APOLLO_RESOLVER_V2_MAX_PAIRS", "120")
    monkeypatch.setenv(TRACE_DIR_ENV, "campaign/out/resolver-v2/traces")
    params = load_params()
    assert params.t_low == 0.30
    assert params.alpha == 1.0
    assert params.top_k_windows == 2
    assert params.max_nli_pairs == 120
    assert params.trace_dir == "campaign/out/resolver-v2/traces"
    # untouched fields keep their defaults
    assert params.t_mid == ResolverV2Params().t_mid
    assert params.max_contradiction == ResolverV2Params().max_contradiction


def test_load_params_malformed_falls_back(monkeypatch):
    """Malformed env values never raise — they fall back to the default."""
    monkeypatch.setenv("APOLLO_RESOLVER_V2_ALPHA", "not-a-float")
    monkeypatch.setenv("APOLLO_RESOLVER_V2_MAX_PAIRS", "many")
    params = load_params()
    assert params.alpha == ResolverV2Params().alpha
    assert params.max_nli_pairs == ResolverV2Params().max_nli_pairs


def test_params_frozen():
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        load_params().alpha = 0.5  # type: ignore[misc]


# --- ResolverV2Result.trace ---------------------------------------------------


def _sample_result() -> ResolverV2Result:
    best = PairScore(
        window_index=2, view_index=1, lexical=0.4, entailment=0.95,
        contradiction=0.01, fused=0.8675,
    )
    node = NodeScore(
        canonical_key="eq.continuity", score=0.8675, credit=0.7,
        source="nli", best=best,
    )
    skipped = NodeScore(
        canonical_key="def.flow_rate", score=0.05, credit=0.0,
        source="lexical_skip", best=None,
    )
    edge = EdgeScore(
        edge_type="USES", from_key="proc.apply_continuity",
        to_key="eq.continuity", credit=0.42, relation_evidence="cooccur",
    )
    return ResolverV2Result(
        node_scores=(node, skipped),
        edge_scores=(edge,),
        node_coverage=0.35,
        edge_coverage=0.42,
        winning_path_index=0,
        grayzone_used=False,
        pair_count=6,
    )


def test_trace_json_round_trip():
    """trace() is JSON-safe and carries the summary/nodes/edges shape the
    artifact + trace-dump consumers (T7) read."""
    trace = _sample_result().trace()
    round_tripped = json.loads(json.dumps(trace))
    assert round_tripped["summary"] == {
        "node_coverage": 0.35,
        "edge_coverage": 0.42,
        "winning_path_index": 0,
        "grayzone_used": False,
        "pair_count": 6,
        "node_count": 2,
        "edge_count": 1,
    }
    assert round_tripped["nodes"][0]["canonical_key"] == "eq.continuity"
    assert round_tripped["nodes"][0]["best"]["entailment"] == 0.95
    assert round_tripped["nodes"][1]["best"] is None  # edge case: skipped node
    assert round_tripped["edges"][0]["relation_evidence"] == "cooccur"


def test_trace_empty_result_json_safe():
    """Edge case: an empty result (no nodes/edges) still round-trips."""
    empty = ResolverV2Result(
        node_scores=(), edge_scores=(), node_coverage=1.0, edge_coverage=1.0,
        winning_path_index=0, grayzone_used=False, pair_count=0,
    )
    round_tripped = json.loads(json.dumps(empty.trace()))
    assert round_tripped["nodes"] == []
    assert round_tripped["edges"] == []
    assert round_tripped["summary"]["node_count"] == 0


# --- package surface ------------------------------------------------------------


def test_package_exports():
    """resolver_v2_enabled is eager; run_resolver_v2 is lazy (engine lands in
    T7); unknown attributes raise AttributeError, not ImportError."""
    assert resolver_v2_pkg.resolver_v2_enabled is resolver_v2_enabled
    assert "run_resolver_v2" in resolver_v2_pkg.__all__
    with pytest.raises(AttributeError):
        _ = resolver_v2_pkg.nonexistent_attribute
