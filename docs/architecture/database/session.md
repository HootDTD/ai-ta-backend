---
doc: database/session
description: Async engine/session factory, the sync-to-async run_async bridge, and the DB-08b per-session RLS enforcement listener.
owns:
  - database/session.py
related:
  - apollo/conversation/routing/auth-deps
  - database/supabase-migrations
last_verified: 2026-07-25
stub: false
---

# database/session — engine, session, RLS

Async engine/session factory + sync bridge + the DB-08b RLS enforcement seam.

## Interface

- `get_db_session()` — FastAPI dependency yielding an `AsyncSession`. **The ONLY
  path that can be RLS-enforced.**
- `get_async_session()` — async ctx-manager for non-FastAPI callers
  (BackgroundTasks, `ai/router/wiring`, workers, scripts, campaign). **Never
  enforces.**
- `run_async(coro)` — runs a coroutine on a persistent daemon-thread event loop
  and blocks; how every sync FastAPI endpoint does DB work.
- `current_request_user_id` — a `contextvars.ContextVar` set by
  [apollo auth `require_user`](../apollo/conversation/routing/auth-deps.md).

## Data flow

Engines are lazy **per event loop** (`_engines` keyed by `id(loop)`,
`_engines_lock`) because asyncpg pools bind to one loop and the process runs two
(uvicorn + the `run_async` bg loop). Connection: `SUPABASE_DB_URL`,
`pool_size=10`, `max_overflow=20`, `pool_pre_ping=True`, `pool_recycle=1800`
(a backstop — the primary conn-drop protection is the checkpoint indexer's
short-session pattern). SQLite URLs erase `app`/`internal` via
`schema_translate_map`.

## Invariants & gotchas

- **Enforcement is gated on HOW the session was created, not ambient context.**
  Only `get_db_session` installs `_install_rls_context_for_session` — a
  **per-session-instance** `after_begin` listener (postgresql dialect only).
- **The listener reads `current_request_user_id` LAZILY**, at first-transaction
  time (autobegin), not at session-mint time. An eager read observed `None` on
  real `/apollo` requests because `Depends` resolves `get_db_session` before the
  identity dep — enforcement never fired. **Load-bearing invariant: every
  enforced route MUST resolve identity before its first query.**
- On fire it issues `SET LOCAL ROLE app_runtime` + bound
  `set_config('request.jwt.claims', …, true)` — both transaction-scoped, so a
  pooled connection never leaks role/claims into the next transaction.
- `get_async_session` sessions stay on the owning (BYPASSRLS) role
  unconditionally — the listener is never installed on them.

## Env flags

- `SUPABASE_DB_URL` (asyncpg or `sqlite+` URL).

## Related

- [apollo/conversation/routing/auth-deps](../apollo/conversation/routing/auth-deps.md)
  (sets the contextvar), [supabase-migrations](supabase-migrations.md) (the
  DB-08b grants migration pairs with this listener). Also see
  `shared-architecture/security.md` for the enforced-vs-owner route inventory.
