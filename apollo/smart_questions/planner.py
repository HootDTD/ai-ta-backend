"""Pure policy for choosing one unresolved reference node or stopping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apollo.ontology import KGGraph

CoverageState = Literal["covered", "partial", "missing", "misconceived"]


@dataclass(frozen=True)
class NodeCoverage:
    node_id: str
    state: CoverageState
    credit: float


def choose_target(
    reference_graph: KGGraph,
    coverage: list[NodeCoverage],
    asked_node_ids: set[str],
) -> str | None:
    """Choose the earliest prerequisite-ready unresolved node.

    A node is eligible exactly once per attempt. Covered nodes and nodes that
    have already received their opportunity are never selected again.
    """
    by_id = {item.node_id: item for item in coverage}
    order = {node.node_id: index for index, node in enumerate(reference_graph.nodes)}
    dependencies: dict[str, set[str]] = {node.node_id: set() for node in reference_graph.nodes}
    for edge in reference_graph.edges:
        if edge.edge_type.value == "DEPENDS_ON":
            # Canonical reference direction is prerequisite -> dependent, while
            # this lookup needs dependent -> {prerequisites}.
            dependencies.setdefault(edge.to_node_id, set()).add(edge.from_node_id)

    eligible = []
    for node in reference_graph.nodes:
        item = by_id.get(node.node_id)
        if item is None or item.state == "covered" or node.node_id in asked_node_ids:
            continue
        prereqs = dependencies.get(node.node_id, set())
        prereqs_ready = all(
            by_id.get(dep) is not None and by_id[dep].state == "covered" for dep in prereqs
        )
        eligible.append((not prereqs_ready, item.credit, order[node.node_id], node.node_id))
    return min(eligible)[-1] if eligible else None
