"""Shared LOCAL Testcontainers Neo4j fixture for WU-3C1 integration tests.

The repo-wide `neo4j_client` fixture in `apollo/conftest.py` connects to LIVE
Neo4j Aura (it loads `.env`), which the WU-3C1 binding constraint forbids
touching and which would SKIP when creds are absent. WU-3C1's Neo4j integration
tests therefore use this distinct `tc_neo4j` fixture, backed by a local
`neo4j:5.25` Testcontainers container (same pattern as `tests/conftest.py`),
so they run green against a container and never reach Aura.

The name is deliberately NOT `neo4j_client` so it does not shadow / collide with
the live-Aura fixture used by the pre-existing tests in this directory.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

_NEO4J_IMAGE = "neo4j:5.25"


@pytest.fixture(scope="session")
def _tc_neo4j_conn():
    """Start a Testcontainers Neo4j once per session (local only; never Aura)."""
    try:
        from testcontainers.neo4j import Neo4jContainer
    except ImportError:  # pragma: no cover - dependency guard
        pytest.skip("testcontainers neo4j module unavailable")
    try:
        container = Neo4jContainer(_NEO4J_IMAGE)
        container.start()
    except Exception as exc:  # pragma: no cover - Docker down
        pytest.skip(f"Docker not available for neo4j test container: {exc}")
    try:
        yield {
            "uri": container.get_connection_url(),
            "user": getattr(container, "username", "neo4j"),
            "password": getattr(container, "password", "password"),
            "database": "neo4j",
        }
    finally:
        container.stop()


@pytest_asyncio.fixture
async def tc_neo4j(_tc_neo4j_conn):
    """Function-scoped apollo Neo4jClient on a freshly-wiped local container."""
    from apollo.persistence.neo4j_client import Neo4jClient

    client = Neo4jClient(
        uri=_tc_neo4j_conn["uri"],
        user=_tc_neo4j_conn["user"],
        password=_tc_neo4j_conn["password"],
        database=_tc_neo4j_conn["database"],
    )

    async def _wipe() -> None:
        async with client.session() as s:
            await s.run("MATCH (n) DETACH DELETE n")

    try:
        await _wipe()
        yield client
    finally:
        await _wipe()
        await client.close()
