"""DB-08b: session-creation-path enforcement gate (no Docker, no Postgres).

Two of database/session.py's DB-08b claims don't need a real database to
verify:

1. ``run_async`` contains no explicit ``current_request_user_id`` handling of
   its own (no snapshot/reapply code, unlike a prior draft of this module).
   Whatever contextvar state is visible inside the scheduled coroutine is
   exactly whatever Python's own ``asyncio.run_coroutine_threadsafe`` ->
   ``loop.call_soon_threadsafe`` machinery captures by default -- which,
   per CPython's ``asyncio.events.Handle.__init__``
   (``context = contextvars.copy_context()``, captured on the CALLING thread
   when the handle is constructed, not on the loop's own thread), DOES carry
   the calling thread's ``.set()`` calls through. This is harmless for RLS
   purposes: DB-08b's enforcement boundary is gated on *which function
   created the session* (``get_db_session`` vs. ``get_async_session`` -- see
   ``_install_rls_context_for_session``), never on whether this contextvar
   happens to be readable in a given coroutine. Every session reachable
   through ``run_async`` is a ``get_async_session()`` session (server.py's
   sync routes never use ``Depends(get_db_session)``), which never installs
   the enforcement listener regardless of contextvar state.
2. ``_install_rls_context_for_session`` is a no-op for any non-``postgresql``
   dialect -- the sqlite+aiosqlite engine the rest of the unit suite runs on
   must never attempt ``SET LOCAL ROLE app_runtime`` (that's not valid SQLite
   and would break every non-DB test in this repo).

The claim that actually matters most for correctness -- that ``get_db_session``
enforces while ``get_async_session`` NEVER does, even when
``current_request_user_id`` is set in the calling context (the exact shape of
a FastAPI ``BackgroundTasks`` callback), needs real asyncpg/Postgres and
lives in ``tests/database/test_db08b_rls_enforcement.py`` instead.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database.session import current_request_user_id, run_async


def test_run_async_carries_stdlib_default_context_capture():
    """Document (not fight) asyncio's default behavior: run_async has no
    contextvar code of its own, so whatever call_soon_threadsafe captures on
    the calling thread is what the scheduled coroutine sees -- contextvars DO
    cross this hop by default. That is a harmless implementation detail, not
    a security property: see the module docstring above and
    database/session.py's "Enforcement boundary" note for why the RLS gate
    never depends on it.
    """
    token = current_request_user_id.set("user-hop-1")
    try:

        async def _read_contextvar() -> str | None:
            return current_request_user_id.get()

        seen = run_async(_read_contextvar())
    finally:
        current_request_user_id.reset(token)

    assert seen == "user-hop-1"


def test_run_async_with_no_identity_set_sees_none():
    """No request context (workers/scripts) -> the coroutine sees None too."""
    assert current_request_user_id.get() is None

    async def _read_contextvar() -> str | None:
        return current_request_user_id.get()

    assert run_async(_read_contextvar()) is None


def test_run_async_setting_identity_inside_the_coroutine_does_not_leak_back():
    """A .set() made inside the scheduled coroutine must not leak onto the
    calling thread's context -- the scheduled callback runs in a COPY of the
    calling thread's context (contextvars.copy_context()), so mutations
    inside it are local to that copy."""
    assert current_request_user_id.get() is None

    async def _set_and_read() -> str | None:
        current_request_user_id.set("user-inside-coro")
        return current_request_user_id.get()

    seen_inside = run_async(_set_and_read())
    assert seen_inside == "user-inside-coro"
    # The calling thread's contextvar is untouched by the coroutine's .set().
    assert current_request_user_id.get() is None


@pytest.mark.asyncio
async def test_install_rls_context_for_session_is_noop_for_sqlite_dialect():
    """The postgresql-only "after_begin" handler must never attach to sqlite.

    If the dialect guard in _install_rls_context_for_session regressed (e.g.
    the check was inverted or dropped), a transaction on this sqlite engine
    with a request identity set would try to run
    ``SET LOCAL ROLE app_runtime``, which is invalid SQLite syntax and would
    raise before reaching the trivial SELECT below. A clean SELECT 1 is the
    behavioral proof of the no-op -- every non-DB test in this repo runs on
    exactly this engine type and would break immediately if this regressed.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from database.session import _install_rls_context_for_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    token = current_request_user_id.set("user-sqlite-noop")
    try:
        async with factory() as session:
            _install_rls_context_for_session(session, engine)
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
    finally:
        current_request_user_id.reset(token)
        await engine.dispose()


@pytest.mark.asyncio
async def test_install_rls_context_for_session_is_noop_with_no_identity_on_sqlite():
    """No request identity resolved, non-postgresql dialect -> still a no-op
    (the dialect gate alone is sufficient here; sqlite never reaches the
    identity read at all)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from database.session import _install_rls_context_for_session

    assert current_request_user_id.get() is None
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            _install_rls_context_for_session(session, engine)
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
    finally:
        await engine.dispose()


def test_install_rls_context_for_session_attaches_unconditionally_on_postgresql():
    """DB-08b fix regression guard (no Docker, no real connection needed):
    for the postgresql dialect, the "after_begin" listener must be attached
    UNCONDITIONALLY -- i.e. even before any request identity has resolved --
    because the identity read now happens lazily, inside the listener, at
    first-transaction time (see database/session.py's
    ``_install_rls_context_for_session`` docstring). Before that fix, this
    function eagerly checked ``current_request_user_id`` and skipped
    attaching the listener entirely when it read None at session-mint time,
    which is exactly what made enforcement inert on real /apollo/* routes
    (get_db_session is frequently minted before identity resolves).

    Constructing a postgresql+asyncpg engine/session does not open a network
    connection (SQLAlchemy is lazy about that), so this stays a pure unit
    test: it only counts registered ``after_begin`` listeners, never
    executes a statement.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from database.session import _install_rls_context_for_session

    assert current_request_user_id.get() is None
    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost/does-not-need-to-exist")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    try:
        before = len(session.sync_session.dispatch.after_begin.listeners)
        _install_rls_context_for_session(session, engine)
        after = len(session.sync_session.dispatch.after_begin.listeners)
        assert after == before + 1
    finally:
        session.sync_session.close()
