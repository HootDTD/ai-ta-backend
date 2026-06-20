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

import logging
import re
from dataclasses import dataclass, field

from apollo.ontology import KGGraph, NodeType

_LOG = logging.getLogger(__name__)

# The parser's "this-response ordinal" reference shape (`edge_resolver._resolve_ref`
# resolves a BARE `n<i>` against the current response's i-th entry). Any context
# node id matching this would be ambiguous with an ordinal, so the builder
# refuses to emit it.
_ORDINAL_REF = re.compile(r"^n\d+$")

# Mirror `_render_graph_context`'s prompt truncation so labels stay short.
_LABEL_MAX = 60


def is_safe_context_id(node_id: str) -> bool:
    """A context node id is safe iff it can never be mistaken for a
    `^n\\d+$` this-response ordinal in `edge_resolver._resolve_ref`.

    Empty ids are unsafe (a ref must be a non-empty token).
    """
    return bool(node_id) and _ORDINAL_REF.match(node_id) is None


@dataclass(frozen=True)
class ContextNode:
    node_id: str  # the existing node's stable id (e.g. "stu_ab12cd34ef56")
    node_type: NodeType  # so the parser can type-check cross-turn edge endpoints
    label: str  # short human label for the prompt's EXISTING GRAPH block


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


# Per-type deterministic label derivation. No LLM — read a short content field
# in priority order, falling back to the node_type name if nothing usable is
# present. Truncated to `_LABEL_MAX` to mirror the prompt's `[:60]` rendering.
def _label_for(node) -> str:
    c = node.content
    nt = node.node_type
    if nt == "equation":
        raw = (getattr(c, "label", "") or getattr(c, "symbolic", "")) or nt
    elif nt in ("condition", "simplification"):
        raw = getattr(c, "applies_when", "") or nt
    elif nt == "definition":
        raw = getattr(c, "concept", "") or nt
    elif nt == "variable_mapping":
        raw = getattr(c, "term", "") or nt
    elif nt == "procedure_step":
        raw = getattr(c, "action", "") or nt
    else:  # pragma: no cover - NodeType is a closed Literal; defensive only
        raw = nt
    return str(raw)[:_LABEL_MAX]


def build_graph_context(graph: KGGraph) -> GraphContext:
    """Project a read `KGGraph` into the minimal prior-attempt `GraphContext`
    the parser needs for cross-turn edge linking.

    - Skips any node whose id is NOT context-safe (would collide with a
      `^n\\d+$` ordinal) — logged (`graph_context_skip`), never silently kept.
    - Label is derived deterministically per node type (no LLM), truncated to
      60 chars to mirror the prompt rendering.
    - Returns a NEW `GraphContext` with an immutable tuple of nodes; never
      mutates the input graph.
    """
    context_nodes: list[ContextNode] = []
    for n in graph.nodes:
        if not is_safe_context_id(n.node_id):
            _LOG.info(
                "graph_context_skip reason=unsafe_id node_id=%s node_type=%s",
                n.node_id, n.node_type,
            )
            continue
        context_nodes.append(
            ContextNode(node_id=n.node_id, node_type=n.node_type, label=_label_for(n))
        )
    return GraphContext(nodes=tuple(context_nodes))
