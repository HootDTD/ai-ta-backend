---
doc: shared/security
description: Auth flow, membership/tenant enforcement, RLS posture, and secrets rules across the backend and both UIs
owns: []
related:
  - ai-ta-backend/_overview
  - ai-ta-backend/domain-data
  - shared/supabase
last_verified: 2026-07-23
stub: false
---

# Security & Authentication

How identity, authorization, and tenant isolation actually work across `ai-ta-backend`,
`ai-ta-student-ui`, and `ai-ta-teacher-ui`. This doc is deliberately honest about what is
enforced at the **API layer** vs the **DB layer** — they are not the same.

## Auth flow (Supabase JWT → backend)

1. **UIs talk to Supabase GoTrue directly via raw `fetch`** — there is no `supabase-js`
   dependency. Both `ai-ta-student-ui/app/lib/auth.ts` and `ai-ta-teacher-ui/app/lib/auth.ts`
   implement identical sign-up / password sign-in / refresh against
   `{NEXT_PUBLIC_SUPABASE_URL}/auth/v1/*` using `NEXT_PUBLIC_SUPABASE_ANON_KEY` as the `apikey`.
2. Sessions are persisted in **`localStorage`** under key `hoot_auth_session_v1`
   (`access_token`, `refresh_token`, `expires_at`, `user_id`, `user_email`). No cookies,
   no SSR auth. `ensureActiveSession()` refreshes when within 30s of expiry.
3. Every backend call sends `Authorization: Bearer <access_token>` (`authHeaders()` in each
   UI's `app/lib/auth.ts`). Caveat: the student UI's `authHeaders()` falls back to the **anon
   key** as the bearer when no session exists — the backend rejects it (no user behind it),
   but be aware the fallback exists.
4. **Backend validates the token remotely, not locally**: `ai-ta-backend/auth.py
   resolve_auth_context()` calls `GET {SUPABASE_URL}/auth/v1/user` with the bearer token.
   There is no local JWT signature verification or JWKS handling. Results are cached
   in-process for 60s (`AUTH_TOKEN_CACHE_TTL_SECONDS`), keyed by SHA-256 of the token.
   Failure → 401. The validated `AuthContext` carries `user_id` + `access_token`.

## Role & membership enforcement (API layer)

- Tenant unit = **course** (`app.courses`, one row per course/class; the API keeps the
  historical `search_space_id` parameter name for this key).
- `app.course_memberships` maps `(user_id UUID → auth.users, course_id, role)` with
  `role IN ('student','teacher')`. ORM: `CourseMembership` in
  `ai-ta-backend/database/models.py`.
- `_require_course_membership()` in `ai-ta-backend/server.py` is the guard: resolve auth
  context → `has_membership(user_id, search_space_id, role)` → 403 if absent. Teacher
  endpoints pass `role="teacher"`.
- **Auto-enroll**: for student-role checks, a missing membership can be auto-created
  (`auto_enroll_student_membership` in `auth.py`), gated by `AUTO_ENROLL_STUDENT_MEMBERSHIP`
  (default on in code; set to `0` in prod env) and optionally scoped by
  `AUTO_ENROLL_SEARCH_SPACE_IDS`.
- Enrollment also happens via **invite links** (`app.course_invites`): teacher-created
  codes with role, max-uses, and expiry.

## Tenant isolation model — where it's actually enforced

**Enforcement is now two layers deep, and the DB layer is deliberately partial.** The
backend's underlying Postgres connection (`SUPABASE_DB_URL`) is still the Supabase
**`postgres` role, which carries the BYPASSRLS attribute**, and API-layer predicates
(handlers filtering by `search_space_id`/owner after `_require_course_membership()`
passes, repositories with explicit course/owner predicates and isolation tests — DB-08a)
remain the primary, always-on defense for **every** request path. **DB-08b
(`database/session.py`, branch `feat/db08b-rls-enforcement`, migration
`20260722120000_db08b_rls_enforcement_grants`) adds RLS as a second, DB-layer backstop
for ONE class of request traffic on top of that**: it does not remove or weaken the
API-layer checks, and it intentionally does not cover every request path (see the
enforced/not-enforced list below).

Note the mechanism precisely: all 18 `app` tables have **FORCE ROW LEVEL SECURITY** (the
`create_app_schema_v1` postcondition asserts it), so it is the *role attribute*, not
table ownership, that exempts the backend. The moment a transaction runs as a
non-BYPASSRLS role, every policy binds. **DB-08b uses exactly that, gated on how the DB
session was created, not on an ambient property of the calling code:**
`database/session.py::get_db_session` (the FastAPI dependency) is the ONLY
session-creation path that can ever install the enforcement hook — when it mints a
session, and only on the `postgresql` dialect, it attaches a **per-session-instance**
SQLAlchemy ORM `"after_begin"` listener to that one session, unconditionally (whether or
not a request identity has resolved yet). The listener fires at the start of every
transaction on that session (SQLAlchemy autobegins on first statement, not at session
construction) and, reading `current_request_user_id` (a contextvar the auth layer sets
once a bearer token resolves — `apollo/auth_deps.py::require_user`) **lazily, at that
moment**, issues `SET LOCAL ROLE app_runtime` plus transaction-local
`request.jwt.claims` injection so `auth.uid()` resolves — both `SET LOCAL`-scoped, so
they revert automatically at COMMIT/ROLLBACK and a pooled connection can never leak role
or claims into the next transaction (verified under concurrent load in
`tests/database/test_db08b_rls_enforcement.py`).

**Post-review correction (was CRITICAL — enforcement was inert on every real request):**
the original design snapshotted `current_request_user_id` *eagerly*, at session-mint
time, and skipped installing the listener entirely if it read `None` then. FastAPI
resolves a route's `Depends` graph in parameter-declaration order (with identical-callable
caching across the whole request), and `get_db_session` is frequently resolved before the
identity dependency that sets the contextvar — so the eager read observed `None` on
effectively every `/apollo/*` request, and every existing test at the time pre-set the
contextvar before minting the session, an ordering the real request path never produces,
so nothing caught it. Reading lazily at first-transaction time instead only depends on the
already-true-almost-everywhere invariant that **every handler resolves identity before its
first query on the session** (one real violation of that invariant was found and fixed in
`apollo/provisioning/problem_generation/api.py`). `tests/database/test_db08b_rls_enforcement.py`
now includes route-level `TestClient` integration tests that drive the real
`get_db_session` + `require_user`/`require_session_owner` dependency graph end to end and
assert `SELECT current_user == 'app_runtime'` — the class of test that would have caught
this.
`database/session.py::get_async_session` **never** installs this listener, regardless of
whether the contextvar happens to still be readable in the calling code — this matters
because a FastAPI `BackgroundTasks` callback runs as a continuation of the SAME
coroutine/Task the request handler ran in, so the contextvar it set is often still
visible there even though the request-scoped session already closed; an engine-wide hook
(rather than this per-session-instance one) would have incorrectly enforced those
BackgroundTasks sessions too. `run_async` (the sync-endpoint bridge) has no
contextvar-handling code of its own — Python's `asyncio.run_coroutine_threadsafe`
captures the calling thread's context by default, so the var is often readable inside a
`run_async`-scheduled coroutine too, but harmlessly: every session reachable through
`run_async` is a `get_async_session()` session.

**Enforced route families:** all of `/apollo/*` (`apollo/api.py`), and the SYNCHRONOUS
part of the `authored-sets` / `problem-generation` provisioning routers
(`apollo/provisioning/authored_sets/api.py`, `apollo/provisioning/problem_generation/api.py`)
— every one of these uses `Depends(get_db_session)`.
**Never enforced (owner role, as before DB-08b):** `server.py`'s legacy sync routes
(`/ask`, `/teacher/upload`, etc., the `run_async` bridge), `ai/router/wiring.py`,
`teacher_upload_worker.py`, every `BackgroundTasks` callback scheduled by the routers
above (they mint their own `get_async_session()` session, deliberately, because the
request-scoped session is already closed by the time they run), `scripts/`, and
`campaign/`. This is a real, intentional gap, not an oversight — see "Known gaps" below.

This takes effect once the branch merges **and** its migration is applied to the target
environment (test, then prod) — see `shared/supabase` for the human-gated remote-apply
step. Recommended staging sequence:
`docs/_archive/handoffs/2026-07-21-db-cutover-runbook.md`.

## RLS posture (DB layer) — target schema

- **`app` schema (18 tables)**: RLS ENABLEd + FORCEd on every table, with 45
  `auth.uid()`-scoped policies (`TO authenticated`) covering member/owner SELECT,
  owner INSERT/UPDATE, and teacher-of-course access patterns via
  `internal.has_course_role()`. `authenticated` holds DML grants; **`anon` and `PUBLIC`
  are fully revoked** on the schema. Of those 45, 32 are the original DB-04 baseline
  and 13 are DB-08c's (`20260723060000_db08c_rls_write_policy_gaps`) write-policy
  additions (`student_progress`, `course_memberships`, `mastery_events`,
  `learner_state`, `tutoring_messages`, `concepts`, `problems`, `documents`,
  `learner_entities`): DB-04's policy set predates DB-08b's enforcement flip and was
  authored against a BYPASSRLS backend, so several `app` tables only ever got a
  SELECT policy. A post-DB-08b e2e run turned up two live defects from that gap —
  every student's first graded attempt 500ing on the `student_progress` INSERT in
  `apollo/persistence/progress_repo.py::load_progress`, and brand-new students'
  auto-enroll `course_memberships` INSERT being silently denied — before this fix.
  **These write policies are authored for the backend running as `app_runtime`
  (DB-08b's enforced request-path role), not for PostgREST/anon access** — neither UI
  ever talks to `app` tables directly (see "Tenant isolation model" above and
  "Enforced route families" for which request paths this write coverage actually
  protects).
- **`internal` schema (10 tables)**: service-only. All privileges revoked from `PUBLIC`,
  `anon`, and `authenticated` (including default privileges for future objects);
  `authenticated`/`app_runtime` get schema USAGE and EXECUTE on
  `internal.has_course_role()` only (policies call it).
- **`app_runtime`** is created `NOLOGIN NOBYPASSRLS` with DML grants mirroring
  `authenticated` — it is the enforcement role DB-08b switches request transactions to.
  `db08b_rls_enforcement_grants` additionally does `GRANT authenticated TO
  app_runtime` (every one of the 45 policies is scoped `TO authenticated`, and
  Postgres RLS `TO <role>` only binds to that literal role or a role that is a
  *member* of it — the per-table DML grants from DB-04 are necessary but not
  sufficient without this membership). **This is a wider blast radius than the
  per-table grants below it**: `app_runtime` thereby inherits *every* privilege
  `authenticated` holds, not just the ones this migration enumerates — including
  whatever the still-present legacy `public` snapshot tables (no RLS) grant
  `authenticated`, and any future `GRANT ... TO authenticated` Supabase applies as
  its own defaults change. The membership is required (it is also what conveys the
  `extensions`/`auth` schema USAGE that `auth.uid()` and the retrieval path need)
  and is not a removable defect — but a future Supabase default-privilege change
  widens `app_runtime` silently, and that should be understood when reasoning about
  its access.
- Also required: `GRANT app_runtime TO <the migration-applying owner role>` (the
  Supabase `postgres` bootstrap role in every real environment) — without it the
  backend's connection cannot `SET LOCAL ROLE app_runtime` inside a request
  transaction. Granted to both the literal `postgres` role (a no-op if that role
  name doesn't exist, e.g. the local Testcontainers harness) and to `CURRENT_USER`
  so the same migration file works unmodified against hosted Supabase and local
  Docker — each grant is guarded on `pg_has_role(..., 'MEMBER')` so it is skipped
  when the target already holds membership (on hosted Supabase, `CURRENT_USER` =
  `postgres`, so without this guard the second grant would be a literal duplicate
  of the first — harmless, just redundant DDL). **Caution for anyone editing this
  migration**: on Postgres 16+, `CREATE ROLE app_runtime` (DB-04) automatically
  makes its creator a member of `app_runtime` with `ADMIN OPTION` but WITHOUT
  `INHERIT`/`SET` rights (`pg_auth_members.set_option = false`) — so
  `pg_has_role(role, 'app_runtime', 'MEMBER')` is TRUE from that alone, even
  though the role still cannot `SET ROLE app_runtime` (that needs `set_option`,
  checked via `pg_has_role(role, 'app_runtime', 'USAGE')`). A guard written against
  `'MEMBER'` looks idempotent but can silently skip the grant that actually confers
  `SET` rights. This migration's `GRANT app_runtime TO CURRENT_USER` statement is
  unconditional specifically to sidestep that trap (the plain `GRANT` always
  upgrades an admin-only membership to a settable one, and re-granting an
  already-settable membership is a harmless no-op) — do not "simplify" it back to
  a `pg_has_role(..., 'MEMBER')`-guarded conditional.
- These policies are the enforcement layer for the request paths DB-08b actually
  covers (see "Tenant isolation model" for the enforced/not-enforced route list)
  once DB-08b is merged and its migration is applied to an environment — owner/
  worker/background-task connections still bypass them by design. Before merge +
  apply, they are dormant for backend traffic (owner connection bypasses them, and
  no PostgREST data path exists) but not dead code.
- **Reports**: `app.ai_usage_reports` is plain SQLAlchemy with owner-scoped routes; a
  cross-user GET returns **404 exactly like a missing row** (deliberate
  anti-enumeration contract — do not "fix" it to 403). The legacy anon-key REST path
  and its intentionally-permissive `anon` policies are gone.
- **Legacy public schema (still live on remotes until cutover)**: the 022 stopgap
  (ENABLE RLS everywhere, anon blocked) remains in force there until the human-run
  legacy teardown script removes the schema post-cutover. **This now matters for real**
  (not just in theory): now that the DB-08b ordering defect above is fixed and
  enforcement actually activates on `/apollo/*` traffic, an `app_runtime` transaction
  inherits every privilege `authenticated` holds (see the `GRANT authenticated TO
  app_runtime` note under "RLS posture" below) — including any grant Supabase or an
  older migration gave `authenticated` on the still-present legacy `public` tables. **DB-16
  (legacy public schema teardown) should land, or be confirmed already applied, on an
  environment BEFORE that environment applies the DB-08b migration** — verify with
  `mcp__supabase(-test)__list_tables`/`get_advisors` on the target project first.
- Neither UI queries app tables via PostgREST — Supabase is auth-only from the browser;
  all data access goes through the FastAPI backend.

## Known gaps (do not silently rely on these being safe)

- **RLS is a request-path-only backstop, not yet deployed anywhere.** DB-08b
  (`feat/db08b-rls-enforcement`) implements and tests the enforcement mechanism, but
  it only takes effect once the branch merges and its migration is applied to an
  environment (test, then prod — a human-gated step, see `shared/supabase`). Until
  then, and always for worker/background/script connections (which never populate
  `current_request_user_id`), isolation is API-layer predicates only (see above).
  (A post-review fix corrected an ordering defect that would otherwise have kept
  this backstop inert even after merge+apply — see the "Tenant isolation model"
  section's "Post-review correction" note above; route-level integration tests now
  prove enforcement actually activates through the real dependency graph.)
- **`/apollo/*` is authenticated.** All routes call `resolve_auth_context` (run off the
  event loop via `asyncio.to_thread`). Session-scoped routes are owner-gated via
  `apollo/auth_deps.require_session_owner`; session creation is membership-gated via
  `require_course_member`. Identity never comes from the request body.
- Tokens live in `localStorage` (XSS-readable), not httpOnly cookies.
- Token-validation cache means a revoked Supabase session stays valid backend-side for up
  to 60s.

## Service-role key & secrets rules

- `SUPABASE_SERVICE_ROLE_KEY` is used **only** by `vendors/supabase_storage.py` (Supabase
  Storage uploads for teacher materials; falls back to `SUPABASE_API_KEY`/anon). It must
  never appear in either UI, in client bundles, or in logs.
- GoTrue auth validation uses the **anon/publishable key** (`SUPABASE_API_KEY` or
  `SUPABASE_ANON_KEY`). No backend code uses the anon key for data access.
- `SUPABASE_DB_URL` (direct Postgres, BYPASSRLS-capable role) is the most powerful
  secret — backend `.env` / Railway env only.
- UIs get only `NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- Never commit `.env`; `validate_required_env()` in `auth.py` fails startup if
  `SUPABASE_DB_URL`, `OPENAI_API_KEY`, or the Supabase URL/key are missing.

## Per-change checklist

When touching auth, RLS, schema, or any endpoint that reads course data:

- [ ] New endpoint resolves identity via `resolve_auth_context` (or documents why not).
- [ ] Course-scoped endpoints go through `_require_course_membership` with the correct
      `role` (teacher endpoints must pass `role="teacher"`).
- [ ] Every query on course data filters by `search_space_id`/owner. DB-08b RLS is a
      request-path-only backstop (see "Tenant isolation model") — it does not run for
      worker/background/script connections, and until it is merged + its migration
      applied to an environment it does not run for request traffic there either;
      never rely on it as the only filter.
- [ ] New `app` table migration includes ENABLE + **FORCE** ROW LEVEL SECURITY and
      explicit policies (the `create_app_schema_v1` postcondition pattern); new
      `internal` tables inherit the revoke-all default privileges.
- [ ] No service-role key or `SUPABASE_DB_URL` in UI code, client bundles, or logs.
- [ ] If touching `/apollo/*`, preserve the auth_deps gates — identity comes from the
      token only (`require_user` / `require_session_owner` / `require_course_member`).
- [ ] Owner-scoped resources keep the 404 anti-enumeration contract (cross-user reads
      look like missing rows, never 403).
- [ ] Schema changes go through the timestamped `supabase/migrations/` chain, rehearsed
      on local Docker; remote application (test `hjevtxdtrkxjcaaexdxt`, then prod
      `uinkseewnxvumrxksnew`) is a human step (see shared/supabase).
