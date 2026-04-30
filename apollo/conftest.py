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
