"""Neo4j test-harness smoke tests for apollo KG integration.

Proves the `Neo4jClient` connects to a real Neo4j container and that the
`neo4j_client` fixture isolates each test with a wiped graph. Runs only when
Docker is available (skips cleanly otherwise). Apollo's KG behaviour is tested
in depth in Phase 4 — this just validates the harness.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_healthcheck(neo4j_client):
    assert await neo4j_client.healthcheck() is True


async def test_write_then_read_node(neo4j_client):
    async with neo4j_client.session() as s:
        await s.run("CREATE (n:Concept {name: $name})", name="vectors")
        result = await s.run(
            "MATCH (n:Concept {name: $name}) RETURN n.name AS name", name="vectors"
        )
        record = await result.single()
    assert record is not None
    assert record["name"] == "vectors"


async def test_graph_is_wiped_between_tests(neo4j_client):
    """The previous test's node must not leak into this one."""
    async with neo4j_client.session() as s:
        result = await s.run("MATCH (n) RETURN count(n) AS c")
        record = await result.single()
    assert record["c"] == 0
