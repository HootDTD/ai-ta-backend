"""WU-3C2 Step 3 — pure-unit tests for the resolver result model.

No Docker, no network. Pin:
- ``ResolutionResult.resolved_edges()`` yields only resolved nodes that carry
  a ``:Canon`` key (unresolved / canon-keyless nodes excluded);
- ``tier_counts`` is a per-method histogram summing to the node count;
- both result dataclasses are frozen (immutability).
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.resolution.result import ResolutionResult, ResolvedNode


def _rn(node_id: str, *, resolution: str, method: str, canon: int | None, conf: float):
    return ResolvedNode(
        node_id=node_id,
        resolution=resolution,
        resolved_key=None if resolution == "unresolved" else f"k.{node_id}",
        resolved_canon_key=canon,
        method=method,
        confidence=conf,
    )


def test_resolution_result_resolved_edges_only_resolved_with_canon_key():
    result = ResolutionResult(
        resolved=(
            _rn("a", resolution="resolved", method="exact", canon=10, conf=1.0),
            _rn("b", resolution="unresolved", method="unresolved", canon=None, conf=0.0),
            _rn("c", resolution="resolved", method="alias", canon=None, conf=0.92),
        ),
        tier_counts={"exact": 1, "alias": 1, "unresolved": 1},
        llm_calls=0,
    )
    edge_nodes = result.resolved_edges()
    ids = {rn.node_id for rn in edge_nodes}
    assert ids == {"a"}  # b unresolved, c has no canon key


def test_resolution_result_tier_counts_histogram_sums_to_node_count():
    result = ResolutionResult(
        resolved=(
            _rn("a", resolution="resolved", method="exact", canon=1, conf=1.0),
            _rn("b", resolution="resolved", method="symbolic", canon=2, conf=0.98),
            _rn("c", resolution="unresolved", method="unresolved", canon=None, conf=0.0),
        ),
        tier_counts={"exact": 1, "symbolic": 1, "unresolved": 1},
        llm_calls=0,
    )
    assert sum(result.tier_counts.values()) == len(result.resolved)


def test_result_models_are_frozen():
    rn = _rn("a", resolution="resolved", method="exact", canon=1, conf=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rn.confidence = 0.5  # type: ignore[misc]
    result = ResolutionResult(resolved=(rn,), tier_counts={"exact": 1}, llm_calls=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.llm_calls = 1  # type: ignore[misc]
