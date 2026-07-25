---
doc: ai-ta-backend/chats/routes
description: The /chats HTTP router — owner-scoped sync endpoints bridging to async CRUD.
owns:
  - chats/routes.py
related:
  - ai-ta-backend/chats/service
  - ai-ta-backend/platform/http-server
  - ai-ta-backend/platform/auth
  - database/models
last_verified: 2026-07-25
stub: false
---

# chats/routes — the /chats HTTP router

An `APIRouter` mounted in server.py. Sync endpoints bridge to async via
`database.session.run_async`, owner-scoped via `auth.resolve_auth_context`.

## Interface

- `GET /chats` — `list_chats`: sessions for the user (title preview + turn_count),
  optionally filtered by `search_space_id`.
- `GET /chats/{chat_id}` — `get_chat`: full transcript (404 on miss).
- `POST /chats/{chat_id}` — `save_chat`: **DESTRUCTIVE upsert** — lock the session
  `FOR UPDATE`, DELETE all `ChatMessage` rows, re-append each payload turn, then
  `refresh_memory_summary`. 400 on `search_space_id` mismatch / missing for new chats.
- `DELETE /chats/{chat_id}` — `delete_chat` (204).
- `_coerce_search_space_id` helper.

## Invariants & gotchas

- Uses `get_async_session` (NOT `get_db_session`) — **not RLS-enforced**;
  authorization is app-layer owner-scoping (every query filters `user_id` +
  `course_id`).
- `save_chat` re-appends turns via `chats/service.append_turn` (attachments
  forwarded; keywords/citations are not re-imported).

## Related

`chats/service`, `platform/http-server` (mount), `platform/auth`,
`database/models`.
