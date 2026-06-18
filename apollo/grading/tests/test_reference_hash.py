"""WU-4B3 — reference_graph_hash determinism + sensitivity pure tests.

The persisted ``apollo_graph_comparison_runs.reference_graph_hash`` must be (a)
STABLE across replays of the SAME reference graph (so old runs stay explainable),
and (b) SENSITIVE to any node/edge/path change (so a teacher edit changes the
hash). These tests pin both, the order-independence, the version prefix, and a
golden digest to lock the serialization. No container, no LLM.
"""

from __future__ import annotations

from apollo.grading.reference_hash import (
    REFERENCE_HASH_VERSION,
    reference_graph_hash,
)
from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)


def _node(key: str, node_type: str = "equation", symbolic: str | None = None) -> CanonicalNode:
    return CanonicalNode(
        canonical_key=key,
        node_type=node_type,  # type: ignore[arg-type]
        source_node_ids=(f"step-{key}",),  # step ids excluded from the hash
        evidence_spans=(),
        symbolic=symbolic,
        method=None,
        confidence=None,
    )


def _edge(from_key: str, to_key: str) -> CanonicalEdge:
    return CanonicalEdge(
        edge_type="DEPENDS_ON",  # type: ignore[arg-type]
        from_key=from_key,
        to_key=to_key,
        provenance="explicit",
    )


def _reference(
    *,
    nodes: tuple[CanonicalNode, ...] | None = None,
    edges: tuple[CanonicalEdge, ...] | None = None,
    paths: tuple[ReferencePathView, ...] | None = None,
) -> ReferenceGraph:
    if nodes is None:
        nodes = (
            _node("eq.bernoulli", "equation", "p+0.5*rho*v**2=c"),
            _node("cond.incompressibility", "condition"),
        )
    if edges is None:
        edges = (_edge("cond.incompressibility", "eq.bernoulli"),)
    if paths is None:
        paths = (ReferencePathView(canonical_keys=("cond.incompressibility", "eq.bernoulli")),)
    return ReferenceGraph(nodes=nodes, edges=edges, paths=paths)


def test_same_graph_same_hash():
    """Two independent constructions of the SAME graph -> equal hashes, AND equal
    to a hard-coded golden digest (pins the serialization)."""
    h1 = reference_graph_hash(_reference())
    h2 = reference_graph_hash(_reference())
    assert h1 == h2
    assert (
        h1
        == "refhash-v1:7539e0f3151bb0326eac081b976d9674de2b87747737e655f53229992be0c222"
    )


def test_edited_node_changes_hash():
    base = reference_graph_hash(_reference())
    edited_nodes = (
        _node("eq.bernoulli_EDITED", "equation", "p+0.5*rho*v**2=c"),
        _node("cond.incompressibility", "condition"),
    )
    assert reference_graph_hash(_reference(nodes=edited_nodes)) != base


def test_edited_node_symbolic_changes_hash():
    base = reference_graph_hash(_reference())
    edited_nodes = (
        _node("eq.bernoulli", "equation", "p+0.5*rho*v**2=DIFFERENT"),
        _node("cond.incompressibility", "condition"),
    )
    assert reference_graph_hash(_reference(nodes=edited_nodes)) != base


def test_edited_edge_changes_hash():
    base = reference_graph_hash(_reference())
    edited = (_edge("eq.bernoulli", "cond.incompressibility"),)  # endpoints swapped
    assert reference_graph_hash(_reference(edges=edited)) != base


def test_edited_path_changes_hash():
    base = reference_graph_hash(_reference())
    edited = (ReferencePathView(canonical_keys=("eq.bernoulli", "cond.incompressibility")),)
    assert reference_graph_hash(_reference(paths=edited)) != base


def test_node_order_independence():
    """Same nodes in a different tuple order -> SAME hash (sorted-canonical)."""
    a = _node("eq.bernoulli", "equation", "p+0.5*rho*v**2=c")
    b = _node("cond.incompressibility", "condition")
    h1 = reference_graph_hash(_reference(nodes=(a, b)))
    h2 = reference_graph_hash(_reference(nodes=(b, a)))
    assert h1 == h2


def test_step_id_rename_keeps_hash_stable():
    """Renaming a step id (source_node_ids) WITHOUT changing the graph shape keeps
    the hash stable — step ids are excluded; the canonical_key is the identity."""
    base = reference_graph_hash(_reference())
    renamed = (
        CanonicalNode(
            canonical_key="eq.bernoulli",
            node_type="equation",  # type: ignore[arg-type]
            source_node_ids=("RENAMED-STEP",),
            evidence_spans=(),
            symbolic="p+0.5*rho*v**2=c",
            method=None,
            confidence=None,
        ),
        _node("cond.incompressibility", "condition"),
    )
    assert reference_graph_hash(_reference(nodes=renamed)) == base


def test_hash_is_version_prefixed():
    h = reference_graph_hash(_reference())
    assert h.startswith(REFERENCE_HASH_VERSION + ":")
    assert REFERENCE_HASH_VERSION == "refhash-v1"
