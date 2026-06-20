"""WU-5B3a-0 — real-Neo4j integration tests for KGStore.read_node_graded_at.

Mirrors ``test_store_graded_at_neo4j.py`` (the WU-3C1 stamp test): the LOCAL
Testcontainers ``tc_neo4j`` fixture (NEVER live Aura), a ``_StubDb`` for the
SQLAlchemy half (the helper never touches Postgres), nodes written directly via
the Neo4j session, NEGATIVE test attempt_ids. ``read_node_graded_at`` is the
durable ``done_ts`` source the WU-5B3a-1 janitor reads + parses tz-aware.

MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of this gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from apollo.knowledge_graph.store import (
    _NODE_CREATE_CYPHER,
    KGStore,
    _node_to_neo4j_props,
)
from apollo.ontology import NODE_LABELS, build_node

# module-level integration mark; asyncio is applied per async test so the pure
# tz-aware test below stays a plain sync function (no spurious asyncio warning).
pytestmark = pytest.mark.integration

_ISO = "2026-06-18T00:00:02+00:00"


class _StubDb:
    """KGStore needs an SQLAlchemy session; read_node_graded_at never uses it."""

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


@pytest.mark.asyncio
async def test_read_node_graded_at_returns_stamped_value(tc_neo4j):
    aid = -701
    await _write_nodes_direct(tc_neo4j, aid, [_eq("n1", aid), _eq("n2", aid)])
    store = KGStore(_StubDb(), tc_neo4j)  # type: ignore[arg-type]
    await store.stamp_graded_at(attempt_id=aid, ts=_ISO)

    out = await store.read_node_graded_at(attempt_id=aid)

    assert set(out.keys()) == {"n1", "n2"}
    assert all(v == _ISO for v in out.values())
    # All nodes of an attempt share ONE done_ts (the janitor relies on this).
    assert len(set(out.values())) == 1


@pytest.mark.asyncio
async def test_read_node_graded_at_empty_subgraph_returns_empty_dict(tc_neo4j):
    store = KGStore(_StubDb(), tc_neo4j)  # type: ignore[arg-type]
    out = await store.read_node_graded_at(attempt_id=-799)
    # The graded_at_missing dead-letter trigger source.
    assert out == {}


@pytest.mark.asyncio
async def test_read_node_graded_at_omits_unstamped_nodes(tc_neo4j):
    aid = -702
    # Write nodes but DO NOT stamp -> graded_at is absent on every node.
    await _write_nodes_direct(tc_neo4j, aid, [_eq("n1", aid), _eq("n2", aid)])
    store = KGStore(_StubDb(), tc_neo4j)  # type: ignore[arg-type]

    out = await store.read_node_graded_at(attempt_id=aid)

    # Both omitted (graded_at is None) — never leaks a None value. Mirrors
    # read_node_created_at's null-omit behavior.
    assert out == {}


def test_graded_at_parse_is_tz_aware():
    # An offset string parses tz-AWARE; a naive string parses tzinfo=None (which
    # would later TypeError on (done_ts - last_evidence_at).days). Locks the
    # contract the WU-5B3a-1 janitor's Δt subtraction depends on.
    assert datetime.fromisoformat("2026-06-18T00:00:02+00:00").tzinfo is not None
    assert datetime.fromisoformat("2026-06-18T00:00:02").tzinfo is None
    # The strings stamp_graded_at writes carry an offset (now(UTC).isoformat()).
    assert "+00:00" in datetime.now(UTC).isoformat()
