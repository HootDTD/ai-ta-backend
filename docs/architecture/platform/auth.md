---
doc: ai-ta-backend/platform/auth
description: auth.py — Supabase JWT validation with an in-memory token cache, course-membership checks, and student auto-enroll
owns:
  - auth.py
related:
  - ai-ta-backend/platform/http-server
  - ai-ta-backend/database/models
  - ai-ta-backend/reports/ai-use-routes
last_verified: 2026-07-25
stub: false
---

# platform/auth — Supabase JWT + membership

Every authenticated endpoint resolves identity here; there is no auth
middleware (see `http-server`).

## Interface

- `AuthContext` — frozen dataclass `(user_id, access_token)`.
- `resolve_auth_context(request) -> AuthContext` — extracts the `Bearer` token,
  checks an SHA-256-keyed in-memory cache (TTL `AUTH_TOKEN_CACHE_TTL_SECONDS`,
  default 60s), else `GET {SUPABASE_URL}/auth/v1/user`; raises `HTTPException 401`
  on a missing/invalid token.
- `has_membership(db, *, user_id, search_space_id, role=None) -> bool` — one
  `CourseMembership` lookup (owned by `database/models`).
- `can_auto_enroll_student(search_space_id) -> bool` — gated by
  `AUTO_ENROLL_STUDENT_MEMBERSHIP` plus the optional
  `AUTO_ENROLL_SEARCH_SPACE_IDS` allowlist.
- `auto_enroll_student_membership(db, *, user_id, search_space_id) -> bool` —
  inserts a default `student` membership; an `IntegrityError` (already enrolled)
  is treated as success.
- `validate_required_env()` — raises `RuntimeError` if `SUPABASE_URL`,
  `SUPABASE_API_KEY`/`ANON_KEY`, `SUPABASE_DB_URL`, or `OPENAI_API_KEY` is
  missing; called from the server startup hook.

## Data flow

Token → SHA-256 cache key → cache hit returns the cached `user_id`; miss hits
the Supabase user endpoint (`requests`, 15s timeout) then caches `(expiry,
user_id)`. Membership/enroll functions take the caller's own `AsyncSession`.

## Invariants & gotchas

- The cache stores a hash of the token, never the token itself.
- Auto-enroll must **commit-then-tolerate**: an already-existing membership
  (student *or* teacher) satisfies access, so `IntegrityError` → rollback →
  `True`; any other failure → rollback → `False`.
- `CourseMembership` is owned by `database/models`; this doc only queries it.

## Env flags

`AUTH_TOKEN_CACHE_TTL_SECONDS`, `AUTO_ENROLL_STUDENT_MEMBERSHIP`,
`AUTO_ENROLL_SEARCH_SPACE_IDS`, plus the `validate_required_env` set
(`SUPABASE_URL`, `SUPABASE_API_KEY`/`SUPABASE_ANON_KEY`, `SUPABASE_DB_URL`,
`OPENAI_API_KEY`).

## Related

Consumers are `http-server` (every guarded route) and `reports/ai-use-routes`.
