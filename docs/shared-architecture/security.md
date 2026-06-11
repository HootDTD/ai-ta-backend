---
doc: shared/security
description: Auth flow, membership/tenant enforcement, RLS posture, and secrets rules across the backend and both UIs
owns: []
related:
  - ai-ta-backend/_overview
  - ai-ta-backend/domain-data
  - shared/supabase
last_verified: 2026-06-10
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

- Tenant unit = **search space** (`aita_search_spaces`, one row per course/class).
- `course_memberships` (migration 006) maps `(user_id UUID → auth.users, search_space_id,
  role)` with `role IN ('student','teacher')`. ORM: `CourseMembership` in
  `ai-ta-backend/database/models.py`.
- `_require_course_membership()` in `ai-ta-backend/server.py` is the guard: resolve auth
  context → `has_membership(user_id, search_space_id, role)` → 403 if absent. Teacher
  endpoints pass `role="teacher"`.
- **Auto-enroll**: for student-role checks, a missing membership can be auto-created
  (`auto_enroll_student_membership` in `auth.py`), gated by `AUTO_ENROLL_STUDENT_MEMBERSHIP`
  (default on in code; set to `0` in prod env per `docs/DATA-FLOW.md`) and optionally
  scoped by `AUTO_ENROLL_SEARCH_SPACE_IDS`.
- Enrollment also happens via **invite links** (`course_invite_links`, migration 008):
  teacher-created codes with role, max-uses, and expiry.

## Tenant isolation model — where it's actually enforced

**Primary enforcement is the API layer.** The backend connects to Postgres via
`SUPABASE_DB_URL` as the **table owner (`postgres`)**, and owners are exempt from RLS unless
`FORCE ROW LEVEL SECURITY` is set (it is not). So every SQLAlchemy query bypasses RLS;
isolation depends on handlers filtering by `search_space_id` after
`_require_course_membership()` passes.

## RLS posture (DB layer) — honest status

- **RLS enabled on all 17 public tables** as of 2026-06-10 via migration
  `022_enable_rls_stopgap.sql` (ENABLE, not FORCE; applied to both test and prod). Net
  effect: the public **anon key can no longer read/write any table over PostgREST** —
  this is what RLS currently buys us, not per-user row scoping for the app itself.
- **Tables with real policies** (`auth.uid()`-scoped, from migrations 005/006/008/015):
  `course_memberships` (self-read), `chat_sessions` / `chat_turns` (owner r/w),
  `teacher_courses` / `teacher_uploads` / `course_invite_links` (teacher-of-course r/w),
  `chat_session_snippets` / `chat_router_decisions` (owner-only). These policies are
  currently **dormant for backend traffic** (owner connection bypasses them); they matter
  only for direct PostgREST access.
- **Enable-only, no policies** (anon fully blocked, backend unaffected): `aita_search_spaces`,
  `aita_documents`, `aita_chunks`, `teacher_upload_jobs`, and all `apollo_*` tables.
- **Exception — `ai_use_reports`**: the one table the backend reads/writes through the
  anon-key REST client (`reports/ai_use/models.py` via `vendors/supabase_client.py`), so 022
  gives it **intentionally permissive** `anon` INSERT/SELECT policies (`WITH CHECK (true)`).
  Tightening is tracked follow-up work (per-table policies + pgTAP in CI).
- Neither UI queries app tables via PostgREST — Supabase is auth-only from the browser;
  all data access goes through the FastAPI backend.

## Known gaps (do not silently rely on these being safe)

- **`/apollo/*` is authenticated as of migration 023 / Phase 1.** All routes call
  `resolve_auth_context` (run off the event loop via `asyncio.to_thread`). Session-scoped
  routes are owner-gated via `apollo/auth_deps.require_session_owner`; session creation
  is membership-gated via `require_course_member`. Identity never comes from the request
  body — `student_id` was removed from all Apollo request schemas in migration 023.
- Tokens live in `localStorage` (XSS-readable), not httpOnly cookies.
- Token-validation cache means a revoked Supabase session stays valid backend-side for up
  to 60s.
- `ai_use_reports` anon policies are wide open by design until the full RLS policy pass.

## Service-role key & secrets rules

- `SUPABASE_SERVICE_ROLE_KEY` is used **only** by `vendors/supabase_storage.py` (Supabase
  Storage uploads for teacher materials; falls back to `SUPABASE_API_KEY`/anon). It must
  never appear in either UI, in client bundles, or in logs.
- Backend REST client (`vendors/supabase_client.py`) and auth validation use the
  **anon/publishable key** (`SUPABASE_API_KEY` or `SUPABASE_ANON_KEY`).
- `SUPABASE_DB_URL` (direct Postgres, owner role) is the most powerful secret — backend
  `.env` / Railway env only.
- UIs get only `NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- Never commit `.env`; `validate_required_env()` in `auth.py` fails startup if
  `SUPABASE_DB_URL`, `OPENAI_API_KEY`, or the Supabase URL/key are missing.

## Per-change checklist

When touching auth, RLS, schema, or any endpoint that reads course data:

- [ ] New endpoint resolves identity via `resolve_auth_context` (or documents why not).
- [ ] Course-scoped endpoints go through `_require_course_membership` with the correct
      `role` (teacher endpoints must pass `role="teacher"`).
- [ ] Every query on course data filters by `search_space_id` — RLS will NOT catch a
      missing filter (backend is owner-connected and RLS-exempt).
- [ ] New table migration includes `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` (keep the
      022 stopgap invariant: anon key blocked by default) and, if PostgREST access is
      intended, explicit policies.
- [ ] No service-role key or `SUPABASE_DB_URL` in UI code, client bundles, or logs.
- [ ] If touching `/apollo/*`, preserve the auth_deps gates — identity comes from the
      token only (`require_user` / `require_session_owner` / `require_course_member`).
- [ ] Mirror schema/RLS changes to both test (`hjevtxdtrkxjcaaexdxt`) and prod
      (`uduxdniieeqbljtwocxy`) and commit the numbered SQL file (see shared/supabase).
