---
doc: reports/ai-use-routes
description: reports/ai_use/routes.py — the AI-use report HTTP endpoints (create, PDF, detail) with owner-scoped, IDOR-safe guards
owns:
  - reports/ai_use/routes.py
  - reports/__init__.py
  - reports/ai_use/__init__.py
related:
  - platform/auth
  - reports/ai-use-service
  - reports/ai-use-models
  - reports/ai-use-pdf
  - chats/service
  - database/session
last_verified: 2026-07-25
stub: false
---

# reports/ai-use-routes — report HTTP endpoints

The router mounted (unprefixed) by `server.py` inside a `try/except`. Two
package `__init__.py` files ride as glue.

## Interface

- `POST /reports/ai-use/{chat_id}` (`create_ai_use_report`) — auth via
  `resolve_auth_context`, loads the caller's own chat, builds the evidence pack +
  `generate_report`, persists an `AIUsageReport`. `CreateReportBody` = `{style,
  length}`.
- `GET /reports/ai-use/{report_id}.pdf` (`get_ai_use_report_pdf`) — renders the
  stored markdown via `render_pdf_from_markdown` and returns a PDF `Response`.
- `GET /reports/ai-use/{report_id}` (`get_ai_use_report_detail`) — detail JSON.
- Ownership-guard helpers: `_load_owned_chat_session`, `_load_chat_for_user`,
  `_get_owned_report`, `_serialize_report`.

## Data flow

`create` resolves the trusted `user_id`, loads the owner-scoped `ChatSession`
(`chats/service`) to read `course_id` off the row (never from the client), builds
evidence (`ai-use-service`), persists (`ai-use-models`). All DB access is bridged
through `database.session.run_async`.

## Invariants & gotchas

- **`user_id` must be the authenticated identity, never client-supplied** — and
  `course_id` is read off the owning chat session, so a caller cannot forge scope.
- `_get_owned_report`'s query is filtered by `user_id`, so someone else's report
  404s exactly like a missing one (no existence leak).
- On `create`: no owned session → 403; oversize truncated evidence → 413;
  generation failure → 500.

## Related

`platform/auth`, `chats/service` + `database/session` (owner-scoped loads), and
the sibling `ai-use-service` / `ai-use-models` / `ai-use-pdf`.
