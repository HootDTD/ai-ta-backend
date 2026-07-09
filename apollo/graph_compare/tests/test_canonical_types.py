"""WU-4A1 Task 3 — frozen-DTO contract for the canonical-space types.

These pin the IMMUTABILITY (coding-style: return new objects, never mutate) and
the field shape of the five canonical dataclasses BEFORE any builder is written.
No DB, no LLM, no network.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
    build_student_canonical,
)
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution.result import ResolutionResult, ResolvedNode


def _node() -> CanonicalNode:
    return CanonicalNode(
        canonical_key="eq.continuity",
        node_type="equation",
        source_node_ids=("s1",),
        evidence_spans=("rho*A1*v1 - rho*A2*v2",),
    )


def test_canonical_node_is_frozen():
    node = _node()
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.canonical_key = "eq.bernoulli"  # type: ignore[misc]


def test_canonical_edge_is_frozen():
    edge = CanonicalEdge(
        edge_type="PRECEDES", from_key="proc.a", to_key="proc.b", provenance="explicit"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.from_key = "proc.c"  # type: ignore[misc]


def test_canonical_graph_is_frozen():
    graph = CanonicalGraph(nodes=(_node(),), edges=(), unresolved_nodes=(), dropped_edge_count=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        graph.dropped_edge_count = 5  # type: ignore[misc]


def test_reference_path_view_is_frozen():
    view = ReferencePathView(canonical_keys=("eq.continuity",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.canonical_keys = ()  # type: ignore[misc]


def test_reference_graph_is_frozen():
    graph = ReferenceGraph(nodes=(_node(),), edges=(), paths=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        graph.nodes = ()  # type: ignore[misc]


def test_canonical_node_defaults():
    """symbolic / method / confidence default to None; evidence/source default
    to empty tuples where applicable."""
    node = CanonicalNode(
        canonical_key="cond.incompressibility",
        node_type="condition",
        source_node_ids=("s1",),
        evidence_spans=("density is constant",),
    )
    assert node.symbolic is None
    assert node.method is None
    assert node.confidence is None


def test_canonical_graph_field_shape():
    """The CanonicalGraph carries the four S_norm fields with the exact types
    WU-4A2 consumes: nodes/edges tuples, unresolved_nodes tuple of (id, text)
    pairs, and an int drop count."""
    node = _node()
    edge = CanonicalEdge(
        edge_type="USES", from_key="proc.a", to_key="eq.continuity", provenance="explicit"
    )
    graph = CanonicalGraph(
        nodes=(node,),
        edges=(edge,),
        unresolved_nodes=(("s9", "some unparsed surface text"),),
        dropped_edge_count=2,
    )
    assert graph.nodes == (node,)
    assert graph.edges == (edge,)
    assert graph.unresolved_nodes == (("s9", "some unparsed surface text"),)
    assert graph.dropped_edge_count == 2


def test_reference_graph_field_shape():
    node = _node()
    view = ReferencePathView(canonical_keys=("eq.continuity", "eq.bernoulli"))
    graph = ReferenceGraph(nodes=(node,), edges=(), paths=(view,))
    assert graph.paths == (view,)
    assert graph.paths[0].canonical_keys == ("eq.continuity", "eq.bernoulli")


# ---------------------------------------------------------------------------
# PHASE 0 — 0.2 explicit canonical merge type. build_student_canonical took
# member_nodes[0].node_type, trusting type-compat guarantees a single type. After
# the resolver type-gate (0.1) a mixed-type merge is impossible, but the merge
# must convert a silent mis-label into an explicit, observable, deterministic
# rule (lowest node_type lexicographically — NodeType is a str Literal with no
# IntEnum priority — then lowest member id) + a WARNING.
# ---------------------------------------------------------------------------


def _sbuild_node(node_id, node_type, content, *, attempt_id=1):
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content=content,
    )


def _sresolved(node_id, key, method="alias", conf=0.92):
    return ResolvedNode(
        node_id=node_id,
        resolution="resolved",
        resolved_key=key,
        resolved_canon_key=None,
        method=method,
        confidence=conf,
    )


def _sresolution(*rns) -> ResolutionResult:
    return ResolutionResult(resolved=tuple(rns), tier_counts={}, llm_calls=0)


def _mixed_type_inputs():
    """Two student nodes resolving to the SAME key but carrying DIFFERENT node
    types (condition vs equation) — fabricated to force the should-never-fire
    disagreement branch."""
    # The LOWEST member id ('a_eq') is the equation so member_nodes[0].node_type
    # == 'equation' — but the deterministic pick is min({condition, equation}) ==
    # 'condition'. This DISCRIMINATES the explicit-merge rule from the old
    # take-first-member behavior (which would return 'equation').
    graph = KGGraph(
        nodes=[
            _sbuild_node(
                "a_eq", "equation", {"symbolic": "A1*v1 = A2*v2", "label": "", "variables": []}
            ),
            _sbuild_node(
                "b_cond", "condition", {"applies_when": "density is constant", "label": ""}
            ),
        ],
        edges=[],
    )
    resolution = _sresolution(
        _sresolved("a_eq", "shared.key"),
        _sresolved("b_cond", "shared.key"),
    )
    return graph, resolution


def test_merge_mixed_member_types_picks_deterministic_explicit_type():
    """Mixed-type members merging to one key -> CanonicalNode.node_type is the
    deterministic lexicographic min of the member types ('condition' < 'equation'
    -> 'condition'), stable across re-runs."""
    graph, resolution = _mixed_type_inputs()
    a = build_student_canonical(graph, resolution)
    b = build_student_canonical(graph, resolution)
    assert len(a.nodes) == 1
    assert a.nodes[0].canonical_key == "shared.key"
    assert a.nodes[0].node_type == "condition"  # min("condition", "equation")
    assert a == b  # deterministic across re-runs


def test_merge_logs_warning_on_type_disagreement(caplog):
    """A type disagreement on a merged key emits a logging.WARNING naming the
    canonical_key and the disagreeing types."""
    graph, resolution = _mixed_type_inputs()
    with caplog.at_level(logging.WARNING, logger="apollo.graph_compare.canonical"):
        build_student_canonical(graph, resolution)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "shared.key" in msg
    assert "condition" in msg and "equation" in msg


def test_merge_uniform_member_types_unchanged(caplog):
    """Control: all members share a type -> node_type is that type and NO warning
    is emitted (protects the pure common path / test_builder_is_pure...)."""
    graph = KGGraph(
        nodes=[
            _sbuild_node("c1", "condition", {"applies_when": "density is constant", "label": ""}),
            _sbuild_node("c2", "condition", {"applies_when": "constant density", "label": ""}),
        ],
        edges=[],
    )
    resolution = _sresolution(
        _sresolved("c1", "cond.incompressibility"),
        _sresolved("c2", "cond.incompressibility"),
    )
    with caplog.at_level(logging.WARNING, logger="apollo.graph_compare.canonical"):
        s = build_student_canonical(graph, resolution)
    assert len(s.nodes) == 1
    assert s.nodes[0].node_type == "condition"
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
