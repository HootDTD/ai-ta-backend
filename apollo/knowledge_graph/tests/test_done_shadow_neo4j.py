"""WU-4C1 — real-Neo4j gate (Testcontainers ``tc_neo4j``).

Uses the LOCAL ``tc_neo4j`` fixture (this dir's conftest), NEVER live Aura.
These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate.

Pins:
  * RESOLVES_TO is idempotent under retry (LOAD-BEARING #5, Neo4j half): the
    shadow chain re-runs ``write_resolution`` and the MERGE produces exactly ONE
    edge (mirrors test_resolution_store_neo4j::test_re_resolution_is_idempotent_merge);
  * ``KGStore.read_node_created_at`` round-trips every node's ``created_at`` (the
    turn_order signal ``read_graph`` strips), and two nodes written in one batch
    share a ``created_at`` (proving the per-turn grouping signal physically exists);
  * an empty subgraph -> ``{}``.
"""

from __future__ import annotations

from typing import Any

import pytest

from apollo.knowledge_graph.resolution_store import write_resolution
from apollo.knowledge_graph.store import (
    _NODE_CREATE_CYPHER,
    KGStore,
    _node_to_neo4j_props,
)
from apollo.ontology import NODE_LABELS, build_node
from apollo.resolution.result import ResolutionResult, ResolvedNode

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_RESOLVED_AT = "2026-06-18T00:00:00+00:00"


class _UnfrozenStubDb:
    """KGStore.write_nodes needs a session id + an unfrozen phase via .execute."""

    async def execute(self, *a: Any, **kw: Any):
        class _R:
            def scalar_one_or_none(self_inner):
                return "TEACHING"

        return _R()


def _eq(node_id: str, attempt_id: int, *, symbolic: str = "rho*A1*v1 - rho*A2*v2"):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": symbolic, "label": node_id, "variables": []},
    )


async def _write_node_direct(neo, attempt_id: int, node) -> None:
    async with neo.session() as s:
        label = NODE_LABELS[node.node_type]
        row = _node_to_neo4j_props(node)
        row["source"] = "parser"
        row["attempt_id"] = attempt_id
        await s.run(_NODE_CREATE_CYPHER[label], rows=[row])


async def _seed_canon(neo, key: int) -> None:
    async with neo.session() as s:
        await s.run(
            "MERGE (c:Canon {key: $key}) SET c.canonical_key = 'eq.continuity'",
            key=key,
        )


async def _resolves_to_edges(neo, attempt_id: int) -> list[dict]:
    async with neo.session() as s:
        result = await s.run(
            "MATCH (n:_KGNode {attempt_id: $aid})-[e:RESOLVES_TO]->(c:Canon) "
            "RETURN c.key AS canon_key",
            aid=attempt_id,
        )
        return [dict(record) async for record in result]


async def test_resolves_to_idempotent_on_retry(tc_neo4j):
    """The shadow retry re-runs write_resolution; the MERGE keeps exactly one
    RESOLVES_TO edge (idempotent supersede)."""
    aid = -4101
    await _write_node_direct(tc_neo4j, aid, _eq("s1", aid))
    await _seed_canon(tc_neo4j, 1)
    result = ResolutionResult(
        resolved=(ResolvedNode("s1", "resolved", "eq.continuity", 1, "exact", 1.0),),
        tier_counts={"exact": 1},
        llm_calls=0,
    )
    first = await write_resolution(tc_neo4j, aid, result, resolved_at=_RESOLVED_AT)
    second = await write_resolution(tc_neo4j, aid, result, resolved_at=_RESOLVED_AT)
    assert first.edges == 1
    assert second.edges == 1
    edges = await _resolves_to_edges(tc_neo4j, aid)
    assert len(edges) == 1


async def test_read_node_created_at_round_trip(tc_neo4j):
    """write_nodes stamps created_at per batch; read_node_created_at returns
    {node_id: created_at} for every node, and a same-batch pair shares the ts."""
    aid = -4102
    store = KGStore(_UnfrozenStubDb(), tc_neo4j)  # type: ignore[arg-type]
    written = await store.write_nodes(
        attempt_id=aid,
        nodes=[_eq("n1", aid), _eq("n2", aid, symbolic="P1 - P2")],
        source="parser",
    )
    assert written == 2

    out = await store.read_node_created_at(attempt_id=aid)
    assert set(out) == {"n1", "n2"}
    # both written in one batch -> same created_at (the per-turn grouping signal)
    assert out["n1"] == out["n2"]
    assert out["n1"]  # non-empty ISO string


async def test_read_node_created_at_empty_subgraph(tc_neo4j):
    store = KGStore(_UnfrozenStubDb(), tc_neo4j)  # type: ignore[arg-type]
    out = await store.read_node_created_at(attempt_id=-4103)
    assert out == {}


async def test_read_node_created_at_skips_null_created_at(tc_neo4j):
    """A legacy node written WITHOUT a created_at prop is OMITTED from the map
    (the absent-key path that build_turn_order tolerates)."""
    aid = -4104
    # write one node WITH created_at (via store.write_nodes) and one WITHOUT
    # (raw Cypher, no created_at prop) — only the former should appear.
    store = KGStore(_UnfrozenStubDb(), tc_neo4j)  # type: ignore[arg-type]
    await store.write_nodes(attempt_id=aid, nodes=[_eq("with_ts", aid)], source="parser")
    async with tc_neo4j.session() as s:
        await s.run(
            "CREATE (n:Equation:_KGNode {attempt_id: $aid, node_id: $nid, "
            "source: 'parser', symbolic: 'x-y', label: 'legacy', variables: []})",
            aid=aid,
            nid="no_ts",
        )
    out = await store.read_node_created_at(attempt_id=aid)
    assert "with_ts" in out
    assert "no_ts" not in out
