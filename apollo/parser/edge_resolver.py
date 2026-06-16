"""WU-2A: resolve LLM-emitted edge refs into validated typed `Edge` objects.

`_resolve_typed_edges` maps each LLM edge (`from_ref`/`to_ref`/`edge_type`/
`provenance`) to a typed `Edge`, enforcing the §6.3 endpoint rules
(EDGE_ALLOWED_PAIRS) at the PARSER output boundary so the parser never emits a
structurally-invalid edge. Every rejected edge is dropped AND logged with a
reason (`parser_edge_rejected`) — no silent drop, no raise.

Ref convention (from the RQ3 spike, kept verbatim): `from_ref`/`to_ref` are
either `"n<i>"` = the i-th entry of THIS response (0-based) or an existing-graph
node id from the supplied `GraphContext`. `"n<i>"` resolution keys on the
ORIGINAL entry index (preserved across skipped/malformed entries) so a dropped
entry never shifts the mapping (risk #1).

Split out of `parser_llm.py` for cohesion + the <800-line / <50-line-function
size budget. Pure logic, no LLM, no I/O.
"""
from __future__ import annotations

import logging

from apollo.ontology import (
    EDGE_ALLOWED_PAIRS,
    Edge,
    EdgeProvenance,
    EdgeType,
    Node,
    NodeType,
)
from apollo.parser.graph_context import GraphContext

_LOG = logging.getLogger(__name__)


def _resolve_ref(
    ref: str,
    *,
    index_to_node: dict[int, Node],
    graph_context: GraphContext | None,
) -> tuple[str | None, NodeType | None]:
    """Resolve an LLM edge ref to (node_id, node_type).

    `"n<i>"` -> the node built from the i-th ORIGINAL entry. A bare id with a
    `graph_context` -> that context node (cross-turn endpoint); the id is
    returned even when its type is unknown, so the caller can distinguish
    "ref unresolvable" (id None) from "endpoint type unknown" (id set, type
    None). Returns (None, None) when neither shape applies.
    """
    # `^n\d+$` is the reserved this-response ordinal namespace;
    # `graph_context.build_graph_context` guarantees context ids never collide
    # (see `graph_context.is_safe_context_id`), so ordinal-first resolution is
    # unambiguous. An out-of-range ordinal returns (None, None) and does NOT
    # fall through to graph_context — the two namespaces are disjoint by
    # construction (WU-2B nit-3 pin; tested in test_edge_resolver_precedence.py).
    if ref.startswith("n") and ref[1:].isdigit():
        node = index_to_node.get(int(ref[1:]))
        if node is None:
            return None, None
        return node.node_id, node.node_type
    if graph_context is not None and ref:
        # The id is "present" (the LLM named it); its type may still be
        # unknown if it isn't in the supplied context.
        return ref, graph_context.type_of(ref)
    return None, None


def _coerce_provenance(raw: object) -> EdgeProvenance:
    """Coerce an LLM-supplied provenance to the safe default when absent/invalid."""
    return "inferred" if raw == "inferred" else "explicit"


def _reject(reason: str, raw: object, **extra: object) -> None:
    """Log a dropped parser edge with its rejection reason (no silent drop)."""
    parts = " ".join(f"{k}=%s" for k in extra)
    _LOG.info(
        "parser_edge_rejected reason=%s " + parts + " edge=%r",
        reason, *extra.values(), raw,
    )


def _build_typed_edge(
    raw: dict,
    *,
    index_to_node: dict[int, Node],
    graph_context: GraphContext | None,
    attempt_id: int,
) -> Edge | None:
    """Validate + construct one typed Edge, or drop+log and return None.

    Runs the §6.3 pair pre-check against EDGE_ALLOWED_PAIRS before
    construction so the parser never emits an edge the `Edge` validator
    would reject.
    """
    from_id, from_type = _resolve_ref(
        str(raw.get("from_ref", "")), index_to_node=index_to_node, graph_context=graph_context,
    )
    to_id, to_type = _resolve_ref(
        str(raw.get("to_ref", "")), index_to_node=index_to_node, graph_context=graph_context,
    )
    if from_id is None or to_id is None:
        return _reject("unresolvable_ref", raw)
    if from_type is None or to_type is None:
        return _reject("unknown_endpoint_type", raw)
    if from_id == to_id:
        return _reject("self_loop", raw)
    try:
        edge_type = EdgeType(raw.get("edge_type"))
    except ValueError:
        return _reject("bad_edge_type", raw)
    if (from_type, to_type) not in EDGE_ALLOWED_PAIRS[edge_type]:
        return _reject("disallowed_pair", raw, pair=(from_type, to_type))
    try:
        return Edge(
            edge_type=edge_type,
            from_node_id=from_id,
            to_node_id=to_id,
            attempt_id=attempt_id,
            source="parser",
            from_node_type=from_type,
            to_node_type=to_type,
            provenance=_coerce_provenance(raw.get("provenance")),
        )
    except ValueError as exc:  # belt-and-braces; the pair pre-check should prevent this
        return _reject("validator_rejected", raw, detail=exc)


def resolve_typed_edges(
    raw_edges: list[dict],
    *,
    index_to_node: dict[int, Node],
    graph_context: GraphContext | None,
    attempt_id: int,
) -> list[Edge]:
    """Resolve LLM `edges` into typed `Edge` objects (rejections dropped + logged)."""
    edges: list[Edge] = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            _reject("malformed_edge", raw)
            continue
        edge = _build_typed_edge(
            raw,
            index_to_node=index_to_node,
            graph_context=graph_context,
            attempt_id=attempt_id,
        )
        if edge is not None:
            edges.append(edge)
    return edges
