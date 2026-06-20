"""WU-3C1 — real-Neo4j integration tests for KGStore.stamp_graded_at.

Uses the LOCAL Testcontainers `tc_neo4j` fixture (this dir's conftest), NOT the
live-Aura `neo4j_client`. Nodes are written directly via the Neo4j session (the
`test_store_neo4j.py` pattern) so the Postgres freeze path is bypassed;
`stamp_graded_at` never touches Postgres, so the store is built with a `_Stub`
session. Test attempt_ids are NEGATIVE.
"""

from __future__ import annotations

from typing import Any

import pytest

from apollo.knowledge_graph.store import (
    _NODE_CREATE_CYPHER,
    KGStore,
    _node_to_neo4j_props,
)
from apollo.ontology import NODE_LABELS, build_node

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _StubDb:
    """KGStore needs an SQLAlchemy session; stamp_graded_at never uses it."""

    async def execute(self, *a: Any, **kw: Any):
        class _R:
            def scalar_one_or_none(self):
                return 1

        return _R()


def _eq(node_id: str, attempt_id: int):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"symbolic": "x - y", "label": node_id},
    )


async def _write_nodes_direct(neo, attempt_id: int, nodes) -> None:
    async with neo.session() as s:
        for n in nodes:
            label = NODE_LABELS[n.node_type]
            row = _node_to_neo4j_props(n)
            row["source"] = "reference"
            row["attempt_id"] = attempt_id
            await s.run(_NODE_CREATE_CYPHER[label], rows=[row])


async def _graded_at_values(neo, attempt_id: int) -> list[Any]:
    async with neo.session() as s:
        result = await s.run(
            "MATCH (n:_KGNode {attempt_id: $aid}) RETURN n.graded_at AS g",
            aid=attempt_id,
        )
        return [record["g"] async for record in result]


async def test_stamp_graded_at_sets_timestamp_on_all_nodes(tc_neo4j):
    aid = -501
    await _write_nodes_direct(tc_neo4j, aid, [_eq("eq1", aid), _eq("eq2", aid)])

    store = KGStore(_StubDb(), tc_neo4j)  # type: ignore[arg-type]
    stamped = await store.stamp_graded_at(attempt_id=aid)

    assert stamped == 2
    values = await _graded_at_values(tc_neo4j, aid)
    assert len(values) == 2
    assert all(isinstance(v, str) and v for v in values)


async def test_stamp_graded_at_idempotent(tc_neo4j):
    aid = -502
    await _write_nodes_direct(tc_neo4j, aid, [_eq("eq1", aid)])

    store = KGStore(_StubDb(), tc_neo4j)  # type: ignore[arg-type]
    first = await store.stamp_graded_at(attempt_id=aid)
    second = await store.stamp_graded_at(attempt_id=aid)

    assert first == 1
    assert second == 1  # overwrite, not duplicate
    values = await _graded_at_values(tc_neo4j, aid)
    assert len(values) == 1  # still exactly one node
    assert values[0]  # graded_at present


async def test_stamp_graded_at_empty_subgraph_returns_zero(tc_neo4j):
    store = KGStore(_StubDb(), tc_neo4j)  # type: ignore[arg-type]
    stamped = await store.stamp_graded_at(attempt_id=-599)
    assert stamped == 0
