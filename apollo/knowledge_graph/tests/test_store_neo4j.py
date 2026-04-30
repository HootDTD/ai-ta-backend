"""Live Neo4j integration tests for KGStore (V3).

Skips if NEO4J_URI / credentials are not set. Test attempts use NEGATIVE
attempt_ids so production data (positive ids) is never touched. Cleanup is
in the conftest fixture.
"""
from __future__ import annotations

import pytest

from apollo.ontology import Edge, EdgeType, KGGraph, build_node


pytestmark = pytest.mark.asyncio


def _eq(node_id: str, attempt_id: int):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"symbolic": "x - y", "label": node_id},
    )


def _proc(node_id: str, attempt_id: int):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"action": f"do {node_id}", "purpose": "p"},
    )


async def test_write_then_read_roundtrip(neo4j_client):
    """write_nodes + write_edges; read_graph returns matching KGGraph."""
    from apollo.persistence.neo4j_client import Neo4jClient

    # Build a tiny attempt directly via the Neo4j session — no SQLAlchemy.
    aid = -101
    nodes = [_eq("eq1", aid), _proc("p1", aid)]
    edges = [Edge(
        edge_type=EdgeType.USES,
        from_node_id="p1", to_node_id="eq1",
        attempt_id=aid, source="reference",
        from_node_type="procedure_step", to_node_type="equation",
    )]

    # Skip the Postgres freeze check by calling the underlying Neo4j paths
    # directly — these tests cover the Neo4j layer in isolation.
    async with neo4j_client.session() as s:
        from apollo.knowledge_graph.store import (
            _NODE_CREATE_CYPHER, _EDGE_CREATE_CYPHER, _node_to_neo4j_props,
            _edge_to_neo4j_row,
        )
        from apollo.ontology import NODE_LABELS

        for n in nodes:
            label = NODE_LABELS[n.node_type]
            row = _node_to_neo4j_props(n)
            row["source"] = "reference"
            await s.run(_NODE_CREATE_CYPHER[label], rows=[row])

        for e in edges:
            row = _edge_to_neo4j_row(e)
            row["attempt_id"] = aid
            row["source"] = "reference"
            await s.run(_EDGE_CREATE_CYPHER[e.edge_type.value], rows=[row])

    # Now read via the high-level API
    from apollo.knowledge_graph.store import KGStore

    # KGStore needs an SQLAlchemy session; for read-only graph ops we can
    # pass None and bypass the freeze checks.
    class _Stub:
        async def execute(self, *a, **kw):
            class R:
                def scalar_one_or_none(self):
                    return 1
            return R()
    store = KGStore(_Stub(), neo4j_client)  # type: ignore[arg-type]
    graph = await store.read_graph(attempt_id=aid)

    assert {n.node_id for n in graph.nodes} == {"eq1", "p1"}
    assert any(
        e.edge_type == EdgeType.USES and e.from_node_id == "p1" and e.to_node_id == "eq1"
        for e in graph.edges
    )

    # Cleanup is automatic via the fixture's < 0 sweep
