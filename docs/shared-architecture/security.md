---
doc: shared/security
description: Auth flow, membership/tenant enforcement, RLS posture, and secrets rules across the backend and both UIs
owns: []
related:
  - ai-ta-backend/_overview
  - ai-ta-backend/domain-data
  - shared/supabase
last_verified: 2026-07-22
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

**Primary enforcement is the API layer.** The backend connects to Postgres via
`SUPABASE_DB_URL` as the Supabase **`postgres` role, which carries the BYPASSRLS
attribute** — so every SQLAlchemy query bypasses RLS today, and isolation depends on
handlers filtering by `search_space_id`/owner after `_require_course_membership()`
passes. Repositories carry explicit course/owner predicates with isolation tests
(DB-08a).

Note the mechanism precisely: all 18 `app` tables have **FORCE ROW LEVEL SECURITY** (the
`create_app_schema_v1` postcondition asserts it), so it is the *role attribute*, not
table ownership, that exempts the backend. The moment a transaction runs as a
non-BYPASSRLS role, every policy binds. **DB-08b (in flight) uses exactly that**: the
request-path engine issues `SET LOCAL ROLE app_runtime` plus transaction-local
`request.jwt.claims` injection so `auth.uid()` resolves, making the policies load-bearing
for backend traffic; worker/background engines stay on the owner role.

## RLS posture (DB layer) — target schema

- **`app` schema (18 tables)**: RLS ENABLEd + FORCEd on every table, with 32
  `auth.uid()`-scoped policies (`TO authenticated`) covering member/owner SELECT,
  owner INSERT/UPDATE, and teacher-of-course access patterns via
  `internal.has_course_role()`. `authenticated` holds DML grants; **`anon` and `PUBLIC`
  are fully revoked** on the schema.
- **`internal` schema (10 tables)**: service-only. All privileges revoked from `PUBLIC`,
  `anon`, and `authenticated` (including default privileges for future objects);
  `authenticated`/`app_runtime` get schema USAGE and EXECUTE on
  `internal.has_course_role()` only (policies call it).
- **`app_runtime`** is created `NOLOGIN NOBYPASSRLS` with DML grants mirroring
  `authenticated` — it is the enforcement role DB-08b switches request transactions to.
- These policies are **dormant for backend traffic until DB-08b lands** (owner
  connection bypasses them, and no PostgREST data path exists). They are not dead code:
  FORCE RLS + the policy set is the enforcement layer DB-08b activates.
- **Reports**: `app.ai_usage_reports` is plain SQLAlchemy with owner-scoped routes; a
  cross-user GET returns **404 exactly like a missing row** (deliberate
  anti-enumeration contract — do not "fix" it to 403). The legacy anon-key REST path
  and its intentionally-permissive `anon` policies are gone.
- **Legacy public schema (still live on remotes until cutover)**: the 022 stopgap
  (ENABLE RLS everywhere, anon blocked) remains in force there until the human-run
  legacy teardown script removes the schema post-cutover.
- Neither UI queries app tables via PostgREST — Supabase is auth-only from the browser;
  all data access goes through the FastAPI backend.

## Known gaps (do not silently rely on these being safe)

- **RLS is not yet enforced for backend traffic** — isolation is API-layer predicates
  until DB-08b merges and deploys (see above).
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
- [ ] Every query on course data filters by `search_space_id`/owner — until DB-08b is
      live, RLS will NOT catch a missing filter (backend runs BYPASSRLS).
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
