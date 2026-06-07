"""Apollo test fixtures.

Loads .env if present so live Neo4j Aura tests pick up NEO4J_* vars.
Provides a `neo4j_client` fixture that skips the test when NEO4J_URI is unset.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

# Load .env once so live tests see NEO4J_*. dotenv is in requirements.
try:
    from dotenv import load_dotenv

    _env = Path(__file__).resolve().parents[1] / ".env"
    if _env.exists():
        load_dotenv(_env, override=False)
except Exception:  # noqa: BLE001 - dotenv is optional in CI envs
    pass


def _neo4j_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE")
    )


@pytest_asyncio.fixture
async def neo4j_client():
    """Live Neo4j Aura client.

    Skips the test when NEO4J_URI is unset. Test attempt_ids must be NEGATIVE
    integers — the post-test cleanup deletes everything with attempt_id < 0
    so production data (positive ids) is never touched.
    """
    if not _neo4j_configured():
        pytest.skip("NEO4J_URI / credentials not set; skipping live Neo4j test")

    from apollo.persistence.neo4j_client import Neo4jClient

    client = Neo4jClient.from_env()
    yield client

    try:
        async with client.session() as s:
            await s.run(
                "MATCH (n:_KGNode) WHERE n.attempt_id < 0 DETACH DELETE n"
            )
    finally:
        await client.close()


@pytest_asyncio.fixture
async def neo4j_test():
    """Ephemeral Neo4j via Testcontainers for deterministic CI.

    Spins a throwaway neo4j:5 container, applies the schema file, yields a
    Neo4jClient pointed at it, and tears the container down after the test.
    Use for concept/problem/ingest tests that are not attempt_id-scoped and
    so cannot rely on the negative-attempt_id cleanup of `neo4j_client`.
    """
    from pathlib import Path
    from testcontainers.neo4j import Neo4jContainer
    from apollo.persistence.neo4j_client import Neo4jClient

    with Neo4jContainer("neo4j:5.20") as container:
        uri = container.get_connection_url()
        client = Neo4jClient(uri=uri, user=container.username,
                             password=container.password, database="neo4j")
        schema = Path("apollo/persistence/neo4j_schema.cypher").read_text()
        async with client.session() as s:
            for raw in schema.split(";"):
                stmt = "\n".join(
                    line for line in raw.splitlines()
                    if line.strip() and not line.strip().startswith("//")
                ).strip()
                if stmt:
                    await s.run(stmt)
        yield client
        await client.close()
