"""Database engine and session factory for AI-TA."""
from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine = None
_session_factory = None

# ---------------------------------------------------------------------------
# Persistent background event loop for sync → async bridging.
#
# asyncio.run() creates *and closes* a new loop each call, which kills the
# asyncpg connection pool.  Instead we keep a single daemon-thread loop alive
# for the lifetime of the process.
# ---------------------------------------------------------------------------
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop
    with _bg_lock:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        _bg_loop = loop
        return loop


def run_async(coro):
    """Run an async coroutine from a sync context using the shared loop.

    Safe to call from FastAPI sync endpoints (AnyIO worker threads) — the
    asyncpg connection pool stays on one loop that never closes.
    """
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def _get_engine():
    global _engine
    if _engine is None:
        database_url = (os.getenv("SUPABASE_DB_URL") or "").strip()
        if not database_url:
            raise RuntimeError("SUPABASE_DB_URL is not set.")
        _engine = create_async_engine(
            database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def get_async_session():
    """Async context manager for a DB session (non-FastAPI callers)."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


__all__ = [
    "get_db_session",
    "get_async_session",
    "run_async",
    "_get_session_factory",
]
