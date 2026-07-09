"""WU-2B nit-3: pin `edge_resolver._resolve_ref` ordinal-vs-context precedence.

`^n\\d+$` is the reserved this-response ordinal namespace. `build_graph_context`
guarantees context ids never collide with it, so ordinal-first resolution is
unambiguous. These pure tests lock that precedence so a future change cannot
silently let a context id shadow an ordinal (or vice-versa).
"""

from __future__ import annotations

from apollo.ontology import KGGraph, build_node
from apollo.parser.edge_resolver import _resolve_ref
from apollo.parser.graph_context import (
    ContextNode,
    GraphContext,
    build_graph_context,
    is_safe_context_id,
)


def _eq(node_id="stu_eq000000000"):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=-1,
        source="parser",
        content={"symbolic": "x - y", "label": "eq"},
    )


def test_ordinal_ref_resolves_to_this_response_node():
    eq = _eq()
    ctx = GraphContext(nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="prev"),))
    nid, ntype = _resolve_ref("n1", index_to_node={1: eq}, graph_context=ctx)
    assert nid == eq.node_id
    assert ntype == "equation"


def test_context_id_resolves_when_not_ordinal_shaped():
    ctx = GraphContext(nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="prev"),))
    nid, ntype = _resolve_ref("eq_prev", index_to_node={}, graph_context=ctx)
    assert nid == "eq_prev"
    assert ntype == "equation"


def test_safe_context_id_never_collides_with_ordinal():
    graph = KGGraph(
        nodes=[
            _eq("stu_eq000000000"),
            build_node(
                node_type="procedure_step",
                node_id="stu_step00000000",
                attempt_id=-1,
                source="parser",
                content={"action": "apply", "purpose": "find"},
            ),
        ]
    )
    ctx = build_graph_context(graph)
    assert ctx.nodes  # builder produced context
    for n in ctx.nodes:
        assert is_safe_context_id(n.node_id)


def test_ordinal_miss_returns_none_not_context():
    """An out-of-range ordinal must NOT fall through to graph_context — the
    `^n\\d+$` namespace is resolved against this-response entries only."""
    eq = _eq()
    ctx = GraphContext(nodes=(ContextNode(node_id="n9", node_type="equation", label="trap"),))
    nid, ntype = _resolve_ref("n9", index_to_node={0: eq}, graph_context=ctx)
    assert nid is None
    assert ntype is None
