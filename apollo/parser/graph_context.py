"""WU-2A: optional prior-attempt graph context for cross-turn edge linking.

`GraphContext` carries the minimal prior-attempt graph the parser's LLM call
needs to reference nodes from earlier turns: a stable id, node type, and a
short label per existing node. It is what WU-2B will build from the live
attempt graph and thread into `parse_utterance`; in WU-2A it is exercised
only by tests (the param defaults to None so `chat.py` is unchanged).

Frozen + tuple (not list) per the repo immutability rule — `type_of` lets
`_resolve_typed_edges` enforce `EDGE_ALLOWED_PAIRS` for cross-turn endpoints
whose type is not present in the current LLM response.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apollo.ontology import NodeType


@dataclass(frozen=True)
class ContextNode:
    node_id: str          # the existing node's stable id (e.g. "stu_ab12cd34ef56")
    node_type: NodeType   # so the parser can type-check cross-turn edge endpoints
    label: str            # short human label for the prompt's EXISTING GRAPH block


@dataclass(frozen=True)
class GraphContext:
    nodes: tuple[ContextNode, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return len(self.nodes) == 0

    def type_of(self, node_id: str) -> NodeType | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n.node_type
        return None
