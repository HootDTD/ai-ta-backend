"""WU-4A2 shared test builders.

The score-math tests build :class:`CanonicalGraph` / :class:`ReferenceGraph`
DIRECTLY (one ring closer than WU-4A1's resolver-driven fixtures): there is no
resolver, no KGGraph, no LLM, no network. Every helper returns a frozen
dataclass so each test is a pure in-memory unit.
"""

from __future__ import annotations

from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.ontology.edges import EdgeProvenance, EdgeType
from apollo.ontology.nodes import NodeType


def cnode(
    key: str,
    node_type: NodeType = "equation",
    *,
    source_node_ids: tuple[str, ...] | None = None,
    evidence_spans: tuple[str, ...] | None = None,
    symbolic: str | None = None,
    method: str | None = "alias",
    confidence: float | None = 0.92,
) -> CanonicalNode:
    """An S_norm canonical node (defaults look like a resolved student node)."""
    return CanonicalNode(
        canonical_key=key,
        node_type=node_type,
        source_node_ids=source_node_ids if source_node_ids is not None else (f"n_{key}",),
        evidence_spans=evidence_spans if evidence_spans is not None else (f"surface for {key}",),
        symbolic=symbolic,
        method=method,
        confidence=confidence,
    )


def rnode(
    key: str, node_type: NodeType = "equation", *, symbolic: str | None = None
) -> CanonicalNode:
    """An R_norm reference node (no student surface text / method / confidence)."""
    return CanonicalNode(
        canonical_key=key,
        node_type=node_type,
        source_node_ids=(f"ref_{key}",),
        evidence_spans=(),
        symbolic=symbolic,
        method=None,
        confidence=None,
    )


def cedge(
    edge_type: EdgeType,
    from_key: str,
    to_key: str,
    *,
    provenance: EdgeProvenance = "explicit",
) -> CanonicalEdge:
    return CanonicalEdge(
        edge_type=edge_type,
        from_key=from_key,
        to_key=to_key,
        provenance=provenance,
    )


def snorm(
    nodes: tuple[CanonicalNode, ...] = (),
    edges: tuple[CanonicalEdge, ...] = (),
    *,
    unresolved_nodes: tuple[tuple[str, str], ...] = (),
    dropped_edge_count: int = 0,
) -> CanonicalGraph:
    return CanonicalGraph(
        nodes=nodes,
        edges=edges,
        unresolved_nodes=unresolved_nodes,
        dropped_edge_count=dropped_edge_count,
    )


def path(*keys: str) -> ReferencePathView:
    return ReferencePathView(canonical_keys=tuple(keys))


def rgraph(
    nodes: tuple[CanonicalNode, ...] = (),
    edges: tuple[CanonicalEdge, ...] = (),
    paths: tuple[ReferencePathView, ...] = (),
) -> ReferenceGraph:
    return ReferenceGraph(nodes=nodes, edges=edges, paths=paths)


def empty_snorm() -> CanonicalGraph:
    """The degenerate empty student graph (§6.1)."""
    return CanonicalGraph(nodes=(), edges=(), unresolved_nodes=(), dropped_edge_count=0)
