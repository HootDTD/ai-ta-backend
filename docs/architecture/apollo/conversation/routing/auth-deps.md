---
doc: apollo/conversation/routing/auth-deps
description: apollo/auth_deps.py — the four async FastAPI auth dependencies for /apollo and the DB-08b RLS ordering invariant
owns:
  - apollo/auth_deps.py
related:
  - apollo/conversation/routing/router
  - database/session
last_verified: 2026-07-25
stub: false
---

# routing/auth-deps — /apollo auth dependencies

`apollo/auth_deps.py` holds the four natively-async FastAPI auth deps every
`/apollo` route depends on (Phase-1 retrofit: routes previously trusted an
identity in the request body). They reuse `auth.py` primitives.

## Interface

- `require_user(request) -> AuthContext` — resolves the bearer token via
  `asyncio.to_thread(resolve_auth_context)` (401 on invalid token, 500 on
  missing `SUPABASE_*` config, 503 when GoTrue is unreachable). Sets the
  `current_request_user_id` contextvar on success.
- `require_course_member(*, db, auth, search_space_id)` — 403 unless the user is
  a member; may auto-enroll a student (`AUTO_ENROLL_STUDENT_MEMBERSHIP`) then
  **defensively re-checks** membership from the DB.
- `require_course_teacher(*, db, auth, search_space_id)` — 403 unless a
  *teacher*-role member; **never auto-enrolls** (auto-enroll only grants the
  student role).
- `require_session_owner(session_id, request, db) -> AuthContext` — 401/403/404
  gate: a session owned by another user returns **404** (masks existence), then
  re-checks course membership on the session's `course_id`.

## Data flow

`router`'s session-scoped routes `Depends(require_session_owner)`, which calls
`require_user` then loads the `TutoringSession`. The `/from_hoot`, `/sessions`,
`/progress`, `/concepts`, `/problems` routes call `require_user` +
`require_course_member` inline; the two teacher routes use `require_course_teacher`.

## Invariants & gotchas

- **DB-08b — identity MUST resolve before the first query on a `get_db_session`
  db.** RLS role enforcement in `database/session` reads
  `current_request_user_id` *lazily*, at transaction begin (first statement).
  FastAPI's dependency-cache can resolve `get_db_session` before `require_user`,
  so a handler that queries `db` before `require_user` runs permanently locks
  that transaction onto the unenforced role (`SET LOCAL ROLE` runs once per txn).
- Deliberately does **not** reuse `server.py`'s sync `_require_course_membership`
  — that helper drives its own event loop via `run_async` and would deadlock
  inside an async endpoint.
- The 404-for-another-user's-session predicate is intentional (existence masking),
  followed by a membership re-check so a removed owner loses access.

## Env flags

- `AUTO_ENROLL_STUDENT_MEMBERSHIP` — gates student auto-enroll in
  `require_course_member` (authority: `auth.py`).

## Related

The RLS lazy-listener contract lives in `database/session`; token/membership
primitives in `shared/security` (`auth.py`); route wiring in `routing/router`.
