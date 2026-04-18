"""Database engine and session factory for AI-TA.

IMPORTANT: The process runs *two* asyncio event loops:

1. Uvicorn's main loop — where native-async FastAPI routes execute
   (e.g. the Apollo endpoints under ``/apollo/*``).
2. A daemon-thread background loop owned by :func:`run_async` — where
   sync FastAPI endpoints bridge into async DB work.

SQLAlchemy's async engine (via asyncpg) binds its connection pool to
the *first* loop that opens a connection on it. If a single shared
engine is used across both loops, later calls from the other loop
surface as::

    RuntimeError: got Future ... attached to a different loop

Fix: maintain one engine + session factory **per event loop**, keyed
by ``id(loop)``. Each loop lazily gets its own engine on first use,
and they never cross-contaminate. This is transparent to callers —
``get_db_session`` (async) and ``run_async`` / ``get_async_session``
both work without changes.
"""
from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Dict, Tuple

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Per-loop engine/session_factory registry. Keyed by id(loop). Values are
# (engine, session_factory). A threading.Lock guards registry mutations —
# the two loops live on different threads, so dict ops must be protected.
_engines: Dict[int, Tuple[AsyncEngine, async_sessionmaker]] = {}
_engines_lock = threading.Lock()

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


def _build_engine() -> AsyncEngine:
    database_url = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not database_url:
        raise RuntimeError("SUPABASE_DB_URL is not set.")
    return create_async_engine(
        database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )


def _get_engine_and_factory_for_current_loop() -> Tuple[AsyncEngine, async_sessionmaker]:
    """Return the (engine, session_factory) bound to the currently-running loop.

    Must be called from inside a running event loop (``get_db_session`` and
    ``get_async_session`` are the only callers, and both are async).
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    pair = _engines.get(key)
    if pair is not None:
        return pair
    with _engines_lock:
        pair = _engines.get(key)
        if pair is not None:
            return pair
        engine = _build_engine()
        factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        _engines[key] = (engine, factory)
        return engine, factory


def _get_session_factory() -> async_sessionmaker:
    """Legacy accessor. Returns the session factory for the running loop.

    Kept for backwards compatibility with existing callers that import
    this symbol. Must be called from inside a running event loop.
    """
    _, factory = _get_engine_and_factory_for_current_loop()
    return factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session."""
    _, factory = _get_engine_and_factory_for_current_loop()
    async with factory() as session:
        yield session


@asynccontextmanager
async def get_async_session():
    """Async context manager for a DB session (non-FastAPI callers)."""
    _, factory = _get_engine_and_factory_for_current_loop()
    async with factory() as session:
        yield session


__all__ = [
    "get_db_session",
    "get_async_session",
    "run_async",
    "_get_session_factory",
]
