"""KGGraph aggregate — single source-of-truth shape for the whole pipeline.

Every backend module that today consumes the bag-shaped dict-of-lists should
consume KGGraph instead. Frontend `ApolloKG` mirrors this shape verbatim.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from pydantic import BaseModel, Field

from apollo.ontology.edges import Edge, EdgeType
from apollo.ontology.nodes import Node, NodeType


class KGGraph(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    # --- Indexed lookups -----------------------------------------------------

    def node_index(self) -> dict[str, Node]:
        """Lookup node by node_id. O(N) build, O(1) per query."""
        return {n.node_id: n for n in self.nodes}

    def by_type(self, node_type: NodeType) -> list[Node]:
        return [n for n in self.nodes if n.node_type == node_type]

    def has_node(self, node_id: str) -> bool:
        return any(n.node_id == node_id for n in self.nodes)

    # --- Graph traversal -----------------------------------------------------

    def outgoing(
        self,
        from_node_id: str,
        edge_type: EdgeType | None = None,
    ) -> list[Edge]:
        if edge_type is None:
            return [e for e in self.edges if e.from_node_id == from_node_id]
        return [
            e for e in self.edges
            if e.from_node_id == from_node_id and e.edge_type == edge_type
        ]

    def incoming(
        self,
        to_node_id: str,
        edge_type: EdgeType | None = None,
    ) -> list[Edge]:
        if edge_type is None:
            return [e for e in self.edges if e.to_node_id == to_node_id]
        return [
            e for e in self.edges
            if e.to_node_id == to_node_id and e.edge_type == edge_type
        ]

    def neighbors(
        self,
        from_node_id: str,
        edge_type: EdgeType,
    ) -> list[Node]:
        idx = self.node_index()
        return [
            idx[e.to_node_id]
            for e in self.outgoing(from_node_id, edge_type)
            if e.to_node_id in idx
        ]

    def precedes_chain(self, *, start_node_id: str | None = None) -> list[Node]:
        """Walk the PRECEDES chain.

        If start_node_id is None, find the head (a procedure_step with no
        incoming PRECEDES edge) and walk from there. Returns nodes in
        traversal order.
        """
        if start_node_id is None:
            heads = [
                n for n in self.by_type("procedure_step")
                if not self.incoming(n.node_id, EdgeType.PRECEDES)
            ]
            if not heads:
                return []
            start_node_id = heads[0].node_id

        idx = self.node_index()
        chain: list[Node] = []
        seen: set[str] = set()
        current = start_node_id
        while current and current not in seen:
            seen.add(current)
            node = idx.get(current)
            if node is None:
                break
            chain.append(node)
            outs = self.outgoing(current, EdgeType.PRECEDES)
            current = outs[0].to_node_id if outs else ""
        return chain

    def topological_order(
        self,
        edge_type: EdgeType,
        node_type: NodeType | None = None,
    ) -> list[Node]:
        """Kahn's algorithm over the subgraph induced by `edge_type`.

        Nodes with no incoming edge of `edge_type` come first. Cycles raise
        ValueError. Filter result to `node_type` if given.
        """
        nodes = list(self.nodes)
        if node_type is not None:
            allowed_ids = {n.node_id for n in nodes if n.node_type == node_type}
        else:
            allowed_ids = {n.node_id for n in nodes}

        in_degree: dict[str, int] = {nid: 0 for nid in allowed_ids}
        adj: dict[str, list[str]] = defaultdict(list)
        for e in self.edges:
            if e.edge_type != edge_type:
                continue
            if e.from_node_id not in allowed_ids or e.to_node_id not in allowed_ids:
                continue
            adj[e.from_node_id].append(e.to_node_id)
            in_degree[e.to_node_id] += 1

        queue = deque([nid for nid, d in in_degree.items() if d == 0])
        order: list[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for to in adj[nid]:
                in_degree[to] -= 1
                if in_degree[to] == 0:
                    queue.append(to)

        if len(order) != len(allowed_ids):
            raise ValueError(
                f"cycle detected in {edge_type} subgraph "
                f"({len(order)}/{len(allowed_ids)} nodes ordered)"
            )

        idx = self.node_index()
        return [idx[nid] for nid in order]

    # --- Subgraph operations -------------------------------------------------

    def merge(self, other: "KGGraph") -> "KGGraph":
        """Combine two graphs by node_id. Later nodes/edges win on conflict."""
        merged_nodes = {n.node_id: n for n in self.nodes}
        for n in other.nodes:
            merged_nodes[n.node_id] = n
        merged_edges = list(self.edges) + list(other.edges)
        return KGGraph(nodes=list(merged_nodes.values()), edges=merged_edges)

    def filter_attempt(self, attempt_id: int) -> "KGGraph":
        return KGGraph(
            nodes=[n for n in self.nodes if n.attempt_id == attempt_id],
            edges=[e for e in self.edges if e.attempt_id == attempt_id],
        )
