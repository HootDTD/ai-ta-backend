"""Apollo V3 KG edge taxonomy.

Exactly four edge types. Each edge has typed (from_node_type, to_node_type)
constraints — see EDGE_ALLOWED_PAIRS. Validation runs at construction time
via Pydantic.

The edge `attempt_id` is required so cascade delete via DETACH DELETE on a
subgraph's nodes also removes their edges, and so per-attempt indexes work.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from apollo.ontology.nodes import NODE_LABELS, NodeType


class EdgeType(StrEnum):
    PRECEDES = "PRECEDES"
    USES = "USES"
    DEPENDS_ON = "DEPENDS_ON"
    SCOPES = "SCOPES"


# Allowed (from_node_type, to_node_type) per edge type. Used by the Edge
# validator AND by the parser to refuse malformed extractions.
EDGE_ALLOWED_PAIRS: dict[EdgeType, set[tuple[NodeType, NodeType]]] = {
    EdgeType.PRECEDES: {("procedure_step", "procedure_step")},
    EdgeType.USES: {("procedure_step", "equation")},
    # DEPENDS_ON is generic — any cross-type edge except self-loops on type
    EdgeType.DEPENDS_ON: {
        (a, b)  # type: ignore[misc]
        for a in NODE_LABELS
        for b in NODE_LABELS
    },
    EdgeType.SCOPES: {
        ("simplification", "equation"),
        ("condition", "equation"),
    },
}


class Edge(BaseModel):
    edge_type: EdgeType
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    attempt_id: int
    source: str = "parser"

    # Resolved by the caller (parser / Problem.to_kg_graph) via node lookup.
    # Required for the pair validator to enforce edge-type constraints.
    from_node_type: NodeType | None = None
    to_node_type: NodeType | None = None

    @model_validator(mode="after")
    def _check_pair(self) -> "Edge":
        if self.from_node_id == self.to_node_id:
            raise ValueError(
                f"edge {self.edge_type}: self-loop ({self.from_node_id}) not allowed"
            )
        if self.from_node_type is not None and self.to_node_type is not None:
            pair = (self.from_node_type, self.to_node_type)
            allowed = EDGE_ALLOWED_PAIRS[self.edge_type]
            if pair not in allowed:
                raise ValueError(
                    f"edge {self.edge_type} not allowed for "
                    f"({self.from_node_type} -> {self.to_node_type}); "
                    f"allowed pairs: {sorted(allowed)}"
                )
        return self
