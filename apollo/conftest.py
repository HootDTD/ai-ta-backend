"""Apollo test fixtures.

Loads .env if present so live Neo4j Aura tests pick up NEO4J_* vars.
Provides a `neo4j_client` fixture that skips the test when NEO4J_URI is unset.

WU-3D: the §8A curriculum-cutover behavioral tests under ``apollo/**/tests`` run
on REAL Postgres (the JSONB scoping JOIN can't run on SQLite). The session-scoped
``_pg_url`` (pgvector Testcontainer) and function-scoped ``db_session`` (savepoint
rollback per test) fixtures live in ``tests/conftest.py``; conftest fixtures are
only visible down their own directory tree, so they are re-exported here to make
them available to ``apollo/`` tests as well. They Docker-skip cleanly when the
daemon is unavailable.

"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

# Re-export the real-PG harness fixtures so apollo/**/tests can request them.
from tests.conftest import _pg_url, db_session  # noqa: F401

# Phase-1 auth retrofit: SQLite + UUID(as_uuid=False) binds must be valid
# UUID strings. Tests use these constants instead of "stu-1"-style ids.
# NOTE: Use UUIDs with non-zero leading hex digits so SQLite does not
# interpret them as numeric literals (e.g. "00000000-..." could be read
# as 0.0 in SQLite's numeric affinity).
TEST_USER_ID = "a0000000-0000-4000-8000-000000000001"
TEST_USER_ID_2 = "b0000000-0000-4000-8000-000000000002"
TEST_SPACE_ID = 1  # integer FK to aita_search_spaces; SQLite tests don't enforce the FK

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
            await s.run("MATCH (n:_KGNode) WHERE n.attempt_id < 0 DETACH DELETE n")
    finally:
        await client.close()
