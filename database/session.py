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

DB-08b: RLS enforcement for request traffic
---------------------------------------------
Enforcement boundary (deliberate, narrow): the ONLY session-creation path
that is ever RLS-enforced is ``get_db_session`` — the FastAPI dependency
used by native-async routes (``/apollo/*``, reports, and any other route
wired on ``Depends(get_db_session)``). ``get_async_session`` (used by
FastAPI ``BackgroundTasks`` callbacks, ``ai/router/wiring.py``'s calls from
the sync-endpoint bridge, ``teacher_upload_worker.py``, scripts, and the
``campaign`` harness) and ``run_async`` (the sync-endpoint bridge itself)
NEVER enforce, unconditionally.

This is implemented as a gate on *how the session was created*, not as an
ambient property of the calling context. That distinction matters: a
FastAPI ``BackgroundTasks`` callback runs as a continuation of the same
coroutine/Task that handled the original request, so ``current_request_user_id``
(the contextvar the auth layer sets once a bearer token resolves — see
``apollo/auth_deps.py::require_user``) is still readable there even though
the request-scoped session has already closed. Likewise, ``run_async``'s
thread hop (``asyncio.run_coroutine_threadsafe``) carries the calling
thread's contextvar state across BY DEFAULT — Python's own
``call_soon_threadsafe`` captures ``contextvars.copy_context()`` on the
calling thread, not the loop's thread, so a sync route that resolved a
request identity would still see it readable inside the coroutine
``run_async`` schedules. If enforcement were wired at the engine level (a
"begin" event that fires for every transaction on every connection checked
out from the engine, regardless of which function created the owning
session), BOTH of these would get incorrectly enforced. Instead,
``get_db_session`` installs a **per-session-instance** "after_begin"
listener only on the ORM ``Session`` it itself creates — ``get_async_session``
never installs anything, so its sessions are unconditionally on the owning
(BYPASSRLS) role no matter what the ambient contextvar reads, no matter how
it got there. ``run_async`` itself has no contextvar-handling code at all —
it doesn't need any, because every session reachable through it is a
``get_async_session()`` session (``server.py``'s legacy sync routes never use
``Depends(get_db_session)``) — see ``docs/architecture/domain-data.md`` and
``docs/shared-architecture/security.md`` for the enforced-vs-owner route
inventory.

Ordering fix (post-review, read ``_install_rls_context_for_session``'s own
docstring for the full mechanism): the listener reads
``current_request_user_id`` LAZILY, inside the nested listener closure, at
the moment the transaction actually begins — NOT eagerly when
``get_db_session`` mints the session object. This matters because FastAPI's
``Depends`` graph resolves in parameter-declaration order (recursively,
depth-first, with identical-callable caching across the whole tree), and
``get_db_session`` is very often declared — and, in several real route
shapes, actually *resolved* — before the identity dependency
(``require_user`` / ``require_session_owner``) that sets the contextvar. An
eager read at mint time therefore observed ``None`` on effectively every
``/apollo/*`` request and enforcement never activated in practice. A lazy
read at first-transaction time instead only requires the invariant every
route handler already follows: resolve identity before issuing the first
query on the session. (One place that invariant was violated —
``apollo/provisioning/problem_generation/api.py``'s ``list_generation_seeds``
and ``create_generation_run``, which queried the concept table before
calling ``require_user`` — was fixed alongside this change; see that file's
git history and ``tests/database/test_db08b_rls_enforcement.py``'s
route-level integration tests, which drive the real dependency graph via
``TestClient`` and would catch a regression of either kind.)

When a ``get_db_session``-minted session's listener fires (once per
transaction, autobegin included), the connection issues
``SET LOCAL ROLE app_runtime`` plus a bound
``set_config('request.jwt.claims', ..., true)`` so the RLS policies
(``auth.uid()``, ``internal.has_course_role()``) see that user for exactly
this transaction. ``SET LOCAL`` / ``set_config(..., true)`` are both
transaction-scoped — they revert automatically at COMMIT/ROLLBACK, so a
pooled connection can never leak role or claims into the next transaction,
whichever function mints the next session on it.

Sessions whose transaction begins with no request user yet resolved
(workers, scripts, campaign, raw sessions, and every ``get_async_session()``
caller — none of which ever reach the listener at all, since it is never
installed on their sessions) stay on the owning role (BYPASSRLS) exactly as
before this change. The listener is only ever installed for the
``postgresql`` dialect — the sqlite+aiosqlite engine used by the non-DB unit
suite is untouched.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Dict, Optional, Tuple

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Set by the auth layer once a request's bearer token resolves to a user id
# (apollo/auth_deps.py::require_user is the only call site). Read ONLY by
# get_db_session -- see the module docstring for why get_async_session
# deliberately never reads it. None means "no request identity resolved yet"
# -- the session behaves exactly as it did before DB-08b (owner role, RLS
# bypass).
current_request_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_request_user_id", default=None
)

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

    DB-08b: deliberately contains NO ``current_request_user_id`` handling of
    its own. Note this does NOT mean the contextvar is invisible inside
    ``coro`` — Python's own ``call_soon_threadsafe`` captures the calling
    thread's context by default, so it usually IS still readable there. That
    is harmless: every DB session reached through this bridge is a
    ``get_async_session()`` session (server.py's legacy sync routes never use
    ``Depends(get_db_session)``), which never enforces RLS regardless of
    contextvar state — see the module docstring's enforcement-boundary note.
    """
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def _install_rls_context_for_session(session: AsyncSession, engine: AsyncEngine) -> None:
    """Attach the DB-08b RLS role/claims handler to ONE session instance.

    Deliberately per-*instance*, not per-engine — see the module docstring's
    "Enforcement boundary" section for why this must not be an engine-level
    (i.e. every-session-off-this-engine) hook. Only ``get_db_session`` calls
    this; ``get_async_session`` must never call it.

    No-op for anything but the postgresql dialect (byte-identical sqlite
    suite).

    DB-08b fix (post-review): the listener is now attached UNCONDITIONALLY
    for the postgresql dialect, and reads ``current_request_user_id`` LAZILY
    -- inside the nested ``_set_rls_context`` closure, at the moment a
    transaction actually begins (SQLAlchemy's ``AsyncSession`` autobegins on
    first statement execution, not at session-construction time) -- instead
    of snapshotting it once, eagerly, right here. This is deliberate: FastAPI
    resolves a route's ``Depends`` graph in parameter-declaration order, and
    ``get_db_session`` is frequently declared (and, via the identical-callable
    dependency cache, frequently *first resolved*) before the identity
    dependency (``require_user`` / ``require_session_owner``) that sets the
    contextvar -- see ``docs/architecture/domain-data.md``'s session-path
    boundary section for the concrete route shapes. Reading eagerly here made
    enforcement depend on Depends-declaration order and silently never
    activated for any real ``/apollo/*`` route. Reading lazily instead makes
    enforcement depend on a much simpler, already-true-everywhere invariant:
    every route resolves identity (``require_user``/``require_session_owner``)
    before it issues its FIRST query on the session -- see
    tests/database/test_db08b_rls_enforcement.py's route-level integration
    tests for the end-to-end proof, through the real dependency graph, of
    both this session-construction-vs-first-query relationship and of the
    inverse (a query issued before identity resolves permanently locks that
    transaction onto the owner/BYPASSRLS role, because ``SET LOCAL ROLE`` only
    ever runs once per transaction, at ``after_begin``).
    """
    if engine.sync_engine.dialect.name != "postgresql":
        return

    @event.listens_for(session.sync_session, "after_begin")
    def _set_rls_context(sync_session, transaction, connection) -> None:
        # Fires once per transaction on THIS session instance (including a
        # second autobegin after an explicit commit mid-request), never for
        # any other session sharing the same pooled connection. Exercised
        # end-to-end against real asyncpg/Postgres in
        # tests/database/test_db08b_rls_enforcement.py.
        #
        # Read lazily, right here, at fire time -- see this function's
        # docstring for why. If no request identity has resolved by the time
        # this transaction begins, stay on the owner (BYPASSRLS) role exactly
        # as before DB-08b.
        user_id = current_request_user_id.get()
        if user_id is None:
            return
        claims = json.dumps({"sub": user_id, "role": "authenticated"})
        connection.execute(text("SET LOCAL ROLE app_runtime"))
        connection.execute(
            text("SELECT set_config('request.jwt.claims', :claims, true)"),
            {"claims": claims},
        )


def _build_engine() -> AsyncEngine:
    database_url = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not database_url:
        raise RuntimeError("SUPABASE_DB_URL is not set.")
    if database_url.startswith("sqlite+"):
        return create_async_engine(
            database_url,
            execution_options={
                "schema_translate_map": {"app": None, "internal": None},
            },
        )
    return create_async_engine(
        database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        # Proactively refresh connections older than 30 min so a long-idle
        # connection is never reused. Backstop only — the real fix is that
        # indexing no longer holds a connection across the embedding loop
        # (see indexing/checkpoint_indexer.py).
        pool_recycle=1800,
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
    """FastAPI dependency that yields a DB session.

    DB-08b: this is the ONLY session-creation path that can ever be
    RLS-enforced — see ``_install_rls_context_for_session`` and the module
    docstring's "Enforcement boundary" section.
    """
    engine, factory = _get_engine_and_factory_for_current_loop()
    async with factory() as session:
        _install_rls_context_for_session(session, engine)
        yield session


@asynccontextmanager
async def get_async_session():
    """Async context manager for a DB session (non-FastAPI callers).

    DB-08b: never RLS-enforced, regardless of ``current_request_user_id`` —
    see the module docstring's "Enforcement boundary" section.
    """
    _, factory = _get_engine_and_factory_for_current_loop()
    async with factory() as session:
        yield session


__all__ = [
    "current_request_user_id",
    "get_db_session",
    "get_async_session",
    "run_async",
    "_get_session_factory",
]
