"""WU-3C2 Steps 11-12 — real-Neo4j integration tests (Testcontainers `tc_neo4j`).

Uses the LOCAL `tc_neo4j` fixture (this dir's conftest), NEVER live Aura. Pin:
- write_resolves_to creates exactly one typed RESOLVES_TO edge with the three
  props, targeting the :Canon node by key;
- persist_resolution_fields then KGStore.read_graph round-trips the typed node
  byte-identically (the four resolution props stripped, content unchanged);
- the four props are physically present on the raw node after the write;
- write_resolution twice is idempotent (MERGE -> same edge count, no dup);
- write_resolution returns the resolved/persisted counts;
- FOLDED WU-3C1 nit: write_nodes scoping props persist on the raw node AND are
  stripped from the reconstructed typed node on read.
"""

from __future__ import annotations

from typing import Any

import pytest

from apollo.knowledge_graph.resolution_store import (
    ResolutionFieldSpec,
    ResolvesToEdgeSpec,
    persist_resolution_fields,
    write_resolution,
    write_resolves_to,
)
from apollo.knowledge_graph.store import (
    _NODE_CREATE_CYPHER,
    KGStore,
    _node_to_neo4j_props,
)
from apollo.ontology import NODE_LABELS, build_node
from apollo.resolution.result import ResolutionResult, ResolvedNode

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_RESOLVED_AT = "2026-06-16T00:00:00+00:00"


class _UnfrozenStubDb:
    """KGStore needs an SQLAlchemy session; for write_nodes it must report a
    valid session id AND an unfrozen phase. Both go through `.execute`."""

    async def execute(self, *a: Any, **kw: Any):
        class _R:
            def scalar_one_or_none(self_inner):
                return "TEACHING"  # session id truthy AND an unfrozen phase

        return _R()


def _eq(node_id: str, attempt_id: int, *, symbolic: str = "rho*A1*v1 - rho*A2*v2"):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"symbolic": symbolic, "label": node_id, "variables": []},
    )


async def _write_node_direct(neo, attempt_id: int, node) -> None:
    async with neo.session() as s:
        label = NODE_LABELS[node.node_type]
        row = _node_to_neo4j_props(node)
        row["source"] = "reference"
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
            "RETURN e.method AS method, e.confidence AS confidence, "
            "e.resolved_at AS resolved_at, c.key AS canon_key",
            aid=attempt_id,
        )
        return [dict(record) async for record in result]


async def _raw_node_props(neo, attempt_id: int, node_id: str) -> dict:
    async with neo.session() as s:
        rec = await (
            await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid, node_id: $nid}) RETURN n AS n",
                aid=attempt_id,
                nid=node_id,
            )
        ).single()
        return dict(rec["n"]) if rec else {}


async def test_write_resolves_to_creates_typed_edge(tc_neo4j):
    aid = -701
    await _write_node_direct(tc_neo4j, aid, _eq("s1", aid))
    await _seed_canon(tc_neo4j, 1)

    written = await write_resolves_to(
        tc_neo4j, aid, [ResolvesToEdgeSpec("s1", 1, "exact", 1.0, _RESOLVED_AT)]
    )
    assert written == 1
    edges = await _resolves_to_edges(tc_neo4j, aid)
    assert len(edges) == 1
    assert edges[0]["method"] == "exact"
    assert edges[0]["confidence"] == 1.0
    assert edges[0]["resolved_at"] == _RESOLVED_AT
    assert edges[0]["canon_key"] == 1


async def test_persist_resolution_fields_round_trip(tc_neo4j):
    aid = -702
    original = _eq("s1", aid)
    await _write_node_direct(tc_neo4j, aid, original)

    await persist_resolution_fields(
        tc_neo4j,
        aid,
        [ResolutionFieldSpec("s1", "resolved", "eq.continuity", "exact", 1.0)],
    )

    store = KGStore(_UnfrozenStubDb(), tc_neo4j)  # type: ignore[arg-type]
    graph = await store.read_graph(attempt_id=aid)
    assert len(graph.nodes) == 1
    read_back = graph.nodes[0]
    # The four resolution props are stripped -> content round-trips identically.
    assert read_back.content.symbolic == original.content.symbolic
    assert not hasattr(read_back.content, "resolution")
    assert not hasattr(read_back.content, "resolved_key")


async def test_resolution_field_props_persist_on_node(tc_neo4j):
    aid = -703
    await _write_node_direct(tc_neo4j, aid, _eq("s1", aid))
    await persist_resolution_fields(
        tc_neo4j,
        aid,
        [ResolutionFieldSpec("s1", "resolved", "eq.continuity", "alias", 0.92)],
    )
    raw = await _raw_node_props(tc_neo4j, aid, "s1")
    assert raw["resolution"] == "resolved"
    assert raw["resolved_key"] == "eq.continuity"
    assert raw["resolution_method"] == "alias"
    assert raw["resolution_confidence"] == 0.92


async def test_unresolved_field_omits_resolved_key(tc_neo4j):
    aid = -704
    await _write_node_direct(tc_neo4j, aid, _eq("s1", aid))
    await persist_resolution_fields(
        tc_neo4j,
        aid,
        [ResolutionFieldSpec("s1", "unresolved", None, "unresolved", 0.0)],
    )
    raw = await _raw_node_props(tc_neo4j, aid, "s1")
    assert raw["resolution"] == "unresolved"
    assert "resolved_key" not in raw  # None-omission


async def test_re_resolution_is_idempotent_merge(tc_neo4j):
    aid = -705
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
    assert second.edges == 1  # MERGE overwrites, never duplicates
    edges = await _resolves_to_edges(tc_neo4j, aid)
    assert len(edges) == 1  # still exactly one edge


async def test_write_resolution_edges_and_fields_counts(tc_neo4j):
    aid = -706
    await _write_node_direct(tc_neo4j, aid, _eq("s1", aid))
    await _write_node_direct(tc_neo4j, aid, _eq("s2", aid, symbolic="P1 - P2"))
    await _seed_canon(tc_neo4j, 1)
    result = ResolutionResult(
        resolved=(
            ResolvedNode("s1", "resolved", "eq.continuity", 1, "exact", 1.0),
            ResolvedNode("s2", "unresolved", None, None, "unresolved", 0.0),
        ),
        tier_counts={"exact": 1, "unresolved": 1},
        llm_calls=0,
    )
    out = await write_resolution(tc_neo4j, aid, result, resolved_at=_RESOLVED_AT)
    assert out.edges == 1  # only the resolved-with-canon node
    assert out.fields == 2  # both nodes get fields (unresolved included)


# ---------------------------------------------------------------------------
# FOLDED WU-3C1 deferred nit — write_nodes scoping props persist + strip on read.
# ---------------------------------------------------------------------------


async def test_write_nodes_scoping_props_persist_and_strip_on_read(tc_neo4j):
    aid = -707
    store = KGStore(_UnfrozenStubDb(), tc_neo4j)  # type: ignore[arg-type]
    created = await store.write_nodes(
        attempt_id=aid,
        nodes=[_eq("s1", aid)],
        source="reference",
        user_id="user-opaque-uuid",
        search_space_id=99,
    )
    assert created == 1

    # Raw node carries the scoping/timestamp metadata...
    raw = await _raw_node_props(tc_neo4j, aid, "s1")
    assert raw["user_id"] == "user-opaque-uuid"
    assert raw["search_space_id"] == 99
    assert raw["created_at"]  # ISO timestamp present

    # ...but the reconstructed typed node's content has none of them.
    graph = await store.read_graph(attempt_id=aid)
    content = graph.nodes[0].content
    assert not hasattr(content, "user_id")
    assert not hasattr(content, "search_space_id")
    assert not hasattr(content, "created_at")
