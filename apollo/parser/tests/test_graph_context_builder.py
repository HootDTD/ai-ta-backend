"""WU-2B: graph_context node-id format guard + `build_graph_context` builder.

Pure-function tests — NO Neo4j, NO LLM, NO network. The builder projects a
read `KGGraph` (the prior-attempt subgraph) into the minimal `GraphContext`
the parser needs to reference earlier-turn nodes for cross-turn edges.

Invariant under test: context node ids never collide with the parser's
`^n\\d+$` this-response ordinal namespace (`edge_resolver._resolve_ref`).
"""

from __future__ import annotations

import logging

from apollo.ontology import KGGraph, build_node
from apollo.parser.graph_context import (
    ContextNode,
    GraphContext,
    build_graph_context,
    is_safe_context_id,
)


def _node(node_type, node_id, content, attempt_id=-1):
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content=content,
    )


# ---------------------------------------------------------------------------
# is_safe_context_id
# ---------------------------------------------------------------------------


def test_is_safe_context_id_rejects_ordinal_shape():
    assert is_safe_context_id("n0") is False
    assert is_safe_context_id("n12") is False
    assert is_safe_context_id("stu_ab12cd34ef56") is True
    assert is_safe_context_id("eq_prev") is True
    assert is_safe_context_id("") is False


def test_is_safe_context_id_allows_n_prefix_with_non_digits():
    # "node5" is safe — only a BARE "n" followed by digits is reserved.
    assert is_safe_context_id("node5") is True
    assert is_safe_context_id("n5a") is True


# ---------------------------------------------------------------------------
# build_graph_context
# ---------------------------------------------------------------------------


def test_build_graph_context_projects_all_node_types():
    graph = KGGraph(
        nodes=[
            _node(
                "equation", "stu_eq000000000", {"symbolic": "A1*v1 - A2*v2", "label": "continuity"}
            ),
            _node("condition", "stu_cond00000000", {"applies_when": "incompressible flow"}),
            _node(
                "simplification",
                "stu_simp0000000",
                {"applies_when": "horizontal pipe", "transformation": "drop rho g h"},
            ),
            _node(
                "definition",
                "stu_def000000000",
                {"concept": "density", "meaning": "mass per volume"},
            ),
            _node("variable_mapping", "stu_var000000000", {"term": "velocity", "symbol": "v"}),
            _node(
                "procedure_step",
                "stu_step00000000",
                {"action": "apply continuity", "purpose": "find v2"},
            ),
        ]
    )
    ctx = build_graph_context(graph)
    assert isinstance(ctx, GraphContext)
    assert len(ctx.nodes) == 6
    by_id = {n.node_id: n for n in ctx.nodes}
    # node_id + node_type preserved
    assert by_id["stu_eq000000000"].node_type == "equation"
    assert by_id["stu_cond00000000"].node_type == "condition"
    assert by_id["stu_step00000000"].node_type == "procedure_step"
    # label derived per type, non-empty
    assert by_id["stu_eq000000000"].label == "continuity"
    assert by_id["stu_cond00000000"].label == "incompressible flow"
    assert by_id["stu_simp0000000"].label == "horizontal pipe"
    assert by_id["stu_def000000000"].label == "density"
    assert by_id["stu_var000000000"].label == "velocity"
    assert by_id["stu_step00000000"].label == "apply continuity"
    assert all(n.label for n in ctx.nodes)


def test_build_graph_context_equation_falls_back_to_symbolic_when_no_label():
    graph = KGGraph(
        nodes=[
            _node("equation", "stu_eq111111111", {"symbolic": "P + rho*g*h", "label": ""}),
        ]
    )
    ctx = build_graph_context(graph)
    assert ctx.nodes[0].label == "P + rho*g*h"


def test_build_graph_context_truncates_label_to_60():
    long_when = "x" * 200
    graph = KGGraph(
        nodes=[
            _node("condition", "stu_condlong0000", {"applies_when": long_when}),
        ]
    )
    ctx = build_graph_context(graph)
    assert len(ctx.nodes[0].label) <= 60


def test_build_graph_context_skips_unsafe_ids(caplog):
    graph = KGGraph(
        nodes=[
            _node("equation", "n5", {"symbolic": "x - y", "label": "bad"}),
            _node("equation", "stu_eq222222222", {"symbolic": "a - b", "label": "good"}),
        ]
    )
    with caplog.at_level(logging.INFO, logger="apollo.parser.graph_context"):
        ctx = build_graph_context(graph)
    ids = {n.node_id for n in ctx.nodes}
    assert "n5" not in ids
    assert "stu_eq222222222" in ids
    assert any(
        "graph_context_skip" in r.getMessage() and "n5" in r.getMessage() for r in caplog.records
    )


def test_build_graph_context_empty_graph_is_empty():
    assert build_graph_context(KGGraph()).is_empty() is True


def test_build_graph_context_does_not_mutate_input():
    nodes = [
        _node("equation", "stu_eq333333333", {"symbolic": "x - y", "label": "eq"}),
    ]
    graph = KGGraph(nodes=nodes)
    before_len = len(graph.nodes)
    before_id = id(graph.nodes)
    build_graph_context(graph)
    assert len(graph.nodes) == before_len
    assert id(graph.nodes) == before_id


def test_build_graph_context_returns_immutable_tuple():
    graph = KGGraph(
        nodes=[
            _node("equation", "stu_eq444444444", {"symbolic": "x - y", "label": "eq"}),
        ]
    )
    ctx = build_graph_context(graph)
    assert isinstance(ctx.nodes, tuple)
    assert isinstance(ctx.nodes[0], ContextNode)
