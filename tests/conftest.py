from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest
import pytest_asyncio


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


openai_stub = types.ModuleType("openai")


class _DummyCompletions:
    def create(self, *args, **kwargs):
        content = '{"markdown":"# Stub report","jsonld":{"@type":"Report"}}'
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )


class _DummyEmbeddings:
    def create(self, *args, **kwargs):
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.0])])


class _DummyResponses:
    """Minimal Responses API stand-in for the streaming solver path.

    solve_with_bundle_stream dispatches on event.type and parses the solver
    JSON from response.output_text.delta payloads, so the stub must emit
    valid solver JSON (steps/final_answers/equations_used/assumptions).
    """

    _SOLVER_JSON = (
        '{"steps": "Stub answer.", "final_answers": {}, '
        '"equations_used": [], "assumptions": [], "not_relevant": false}'
    )

    def create(self, *args, **kwargs):
        if kwargs.get("stream"):
            return iter(
                [
                    types.SimpleNamespace(
                        type="response.output_text.delta", delta=self._SOLVER_JSON
                    ),
                    types.SimpleNamespace(type="response.completed"),
                ]
            )
        return types.SimpleNamespace(output_text=self._SOLVER_JSON)


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=_DummyCompletions())
        self.embeddings = _DummyEmbeddings()
        self.responses = _DummyResponses()


openai_stub.OpenAI = _DummyOpenAI
sys.modules["openai"] = openai_stub


@pytest.fixture(autouse=True)
def _mock_supabase(monkeypatch):
    """Route vendors.supabase_client through the shared in-memory mock.

    The PostgREST-style mock now lives in tests/support/supabase_mock.py (shared
    with tests/functions-tests/conftest.py). The root suite uses the no-auto-id
    variant to preserve its historical insert/upsert behaviour.
    """
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")

    from tests.support.supabase_mock import SupabaseMock

    mock = SupabaseMock(auto_id=False)
    mock.install(monkeypatch)
    yield
    mock.reset()


# ---------------------------------------------------------------------------
# Real Postgres + pgvector test harness (Phase 1, docs/TESTING-CI-PLAN.md).
#
# pgvector's `<=>` / `<->` operators and HNSW indexes do not exist in SQLite,
# so the retrieval/indexing layer can only be tested against real Postgres.
# We spin up one ephemeral `pgvector/pgvector:pg16` container per test session
# (Testcontainers), create the schema once, then give each test a
# transactionally-isolated session that rolls back on teardown.
#
# If Docker isn't running, the `_pg_url` fixture skips cleanly so the rest of
# the suite stays green — DB tests light up automatically once Docker is up.
# ---------------------------------------------------------------------------

PGVECTOR_IMAGE = "pgvector/pgvector:pg16"

# NOTE: we deliberately do NOT call pgvector.asyncpg.register_vector here.
# pgvector's SQLAlchemy `Vector` type already serializes lists to the pgvector
# text wire format (e.g. '[1.0,0.0,...]') and parses results back. Registering
# the asyncpg binary codec on top of that double-encodes the value and asyncpg
# raises "could not convert string to float". register_vector is only for RAW
# asyncpg queries that bind Python lists directly, which the ORM never does.


@pytest.fixture(scope="session")
def _pg_url() -> str:
    """Start a pgvector container and create the schema once per session.

    Returns an asyncpg SQLAlchemy URL. Skips the whole DB-backed test set if
    Docker is unavailable.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dependency guard
        pytest.skip("testcontainers not installed (pip install -r requirements-test.txt)")

    try:
        container = PostgresContainer(PGVECTOR_IMAGE)
        container.start()
    except Exception as exc:  # Docker daemon down / image pull failure
        pytest.skip(f"Docker not available for pgvector test container: {exc}")

    url = container.get_connection_url(driver="asyncpg")

    async def _setup() -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from database.models import Base

        # No vector codec on the setup engine: its first connection is the one
        # that runs CREATE EXTENSION, so the `vector` type doesn't exist yet at
        # connect time. DDL is plain text and needs no codec anyway.
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())
    try:
        yield url
    finally:
        container.stop()


@pytest_asyncio.fixture
async def db_session(_pg_url):
    """Function-scoped AsyncSession on real pgvector, rolled back after each test.

    Uses a per-test engine (NullPool) so the connection is created on the
    current test's event loop — avoiding the cross-loop pool issues documented
    in ``database/session.py``. The outer transaction is never committed:
    ``join_transaction_mode="create_savepoint"`` lets test code call
    ``commit()`` (it commits to a SAVEPOINT), and teardown rolls the whole
    thing back, leaving the schema pristine for the next test.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(_pg_url, poolclass=NullPool)

    conn = await engine.connect()
    trans = await conn.begin()
    session = AsyncSession(
        bind=conn,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        if trans.is_active:
            await trans.rollback()
        await conn.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Neo4j test harness for apollo KG integration (prod uses Neo4j Aura).
# Same Docker-guarded skip pattern as pgvector. Each test gets a wiped graph.
# ---------------------------------------------------------------------------

NEO4J_IMAGE = "neo4j:5.25"


@pytest.fixture(scope="session")
def _neo4j_conn():
    """Start a Neo4j container once per session. Skips if Docker is unavailable."""
    try:
        from testcontainers.neo4j import Neo4jContainer
    except ImportError:  # pragma: no cover - dependency guard
        pytest.skip("testcontainers neo4j module unavailable")

    try:
        container = Neo4jContainer(NEO4J_IMAGE)
        container.start()
    except Exception as exc:  # Docker daemon down / image pull failure
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
async def neo4j_client(_neo4j_conn):
    """Function-scoped apollo Neo4jClient on a freshly-wiped graph."""
    from apollo.persistence.neo4j_client import Neo4jClient

    client = Neo4jClient(
        uri=_neo4j_conn["uri"],
        user=_neo4j_conn["user"],
        password=_neo4j_conn["password"],
        database=_neo4j_conn["database"],
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
