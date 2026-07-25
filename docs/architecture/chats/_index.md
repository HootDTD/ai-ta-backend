---
doc: ai-ta-backend/chats/_index
description: Router for chat-session persistence, rolling memory, and the retrieval bundle cache.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# chats — session persistence, memory, bundle cache

Chat sessions persist student conversations with rolling memory so `/ask`
follow-ups have context; the bundle cache lets the retrieval-mode router
(NONE/AUGMENT) reuse a prior turn's snippets instead of re-running pgvector.
Carved out of the retired domain-data (which kept database/supabase/reports).

## Leaves
| Doc | One-liner · owns |
|---|---|
| [service](service.md) | session/turn CRUD + memory summarization (+package marker) · `chats/service.py`, `chats/__init__.py` |
| [routes](routes.md) | the /chats HTTP router (owner-scoped) · `chats/routes.py` |
| [bundle-cache](bundle-cache.md) | retrieval-mode session cache · `chats/bundle_cache.py` |

## Cross-cutting invariants
- **The chat-turn model is `ChatMessage`** (`app.chat_messages`) — the old
  `ChatTurn` / `chat_turns` was renamed in the DB redesign; all three leaves use
  `ChatMessage`. The `ChatSession` / `ChatMessage` / `ChatSessionSnippet` ORM is
  owned by `database/models`.
- These endpoints/services use `get_async_session`, so they are **not
  RLS-enforced** — every query owner-scopes on `user_id` + `course_id`.

## Related domains
`rag-pipeline/_index` (the router + `/ask` memory consume this domain via
`router-wiring`), `knowledge/_index`, `database/models`.
