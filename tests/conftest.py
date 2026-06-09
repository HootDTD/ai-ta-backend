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


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=_DummyCompletions())
        self.embeddings = _DummyEmbeddings()


openai_stub.OpenAI = _DummyOpenAI
sys.modules["openai"] = openai_stub


_sb_store: dict[str, list[dict]] = {}


def _sb_reset() -> None:
    _sb_store.clear()


def _sb_select(table: str, params: dict | None = None):
    params = params or {}
    rows = list(_sb_store.get(table, []))
    for key, val in params.items():
        if key in ("select", "order", "limit", "on_conflict"):
            continue
        if isinstance(val, str) and val.startswith("eq."):
            target = val[3:]
            rows = [r for r in rows if str(r.get(key, "")) == target]
    order = params.get("order", "")
    if order:
        field = order.split(".")[0]
        desc = "desc" in order
        rows.sort(key=lambda r: r.get(field, ""), reverse=desc)
    limit = params.get("limit")
    if limit:
        rows = rows[: int(limit)]
    return rows


def _sb_select_one(table: str, params: dict | None = None):
    rows = _sb_select(table, params)
    return rows[0] if rows else None


def _sb_insert(table: str, data):
    if isinstance(data, dict):
        data = [data]
    _sb_store.setdefault(table, [])
    for row in data:
        _sb_store[table].append(dict(row))
    return list(data)


def _sb_upsert(table: str, data, on_conflict: str = "id"):
    if isinstance(data, dict):
        data = [data]
    rows = _sb_store.setdefault(table, [])
    for row in data:
        found = None
        for idx, existing in enumerate(rows):
            if existing.get(on_conflict) == row.get(on_conflict):
                found = idx
                break
        if found is None:
            rows.append(dict(row))
        else:
            rows[found].update(row)
    return list(data)


def _sb_update(table: str, match_params: dict, data: dict):
    rows = _sb_store.get(table, [])
    out = []
    for row in rows:
        matched = True
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(row.get(key, "")) != val[3:]:
                    matched = False
                    break
        if matched:
            row.update(data)
            out.append(row)
    return out


def _sb_delete(table: str, match_params: dict):
    rows = _sb_store.get(table, [])
    keep = []
    for row in rows:
        matched = True
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(row.get(key, "")) != val[3:]:
                    matched = False
                    break
        if not matched:
            keep.append(row)
    _sb_store[table] = keep


def _sb_rpc(function_name: str, params: dict, *, timeout: int = 30):
    return []


@pytest.fixture(autouse=True)
def _mock_supabase(monkeypatch):
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    _sb_reset()
    import vendors.supabase_client as sb_mod

    monkeypatch.setattr(sb_mod, "select", _sb_select)
    monkeypatch.setattr(sb_mod, "select_one", _sb_select_one)
    monkeypatch.setattr(sb_mod, "insert", _sb_insert)
    monkeypatch.setattr(sb_mod, "upsert", _sb_upsert)
    monkeypatch.setattr(sb_mod, "update", _sb_update)
    monkeypatch.setattr(sb_mod, "delete", _sb_delete)
    monkeypatch.setattr(sb_mod, "rpc", _sb_rpc)
    yield
    _sb_reset()


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
