---
doc: ai-ta-backend/chats/service
description: Chat session/turn CRUD primitives + rolling memory summarization (async).
owns:
  - chats/service.py
  - chats/__init__.py
related:
  - ai-ta-backend/chats/routes
  - ai-ta-backend/chats/bundle-cache
  - ai-ta-backend/rag-pipeline/router-wiring
  - ai-ta-backend/reports/ai-use-routes
  - database/models
last_verified: 2026-07-25
stub: false
---

# chats/service — session/turn CRUD + memory

Thin async persistence primitives on a request-scoped `AsyncSession`.
`chats/__init__.py` is an empty package marker. **The chat-turn model is
`ChatMessage`** (post-DB-redesign rename — the old `ChatTurn`/`chat_turns` is gone).

## Interface

- `build_memory_context(summary, turns: list[ChatMessage]) -> str` — formats
  "Conversation summary:" + "Recent conversation turns:" for prompt injection.
- `get_chat_session_for_user` / `get_or_create_chat_session_for_user` /
  `delete_chat_session_for_user`.
- `list_recent_turns(..., limit=MEMORY_WINDOW_TURNS) -> list[ChatMessage]` (last N,
  ascending).
- `append_turn(..., role, content, ..., keywords=None) -> ChatMessage`.
- `refresh_memory_summary(...)`, `_summarize_turns_for_memory`,
  `serialize_chat_session -> dict`.

## Data flow

`append_turn` takes `SELECT ... FOR UPDATE` on the session row, then assigns
`turn_index = max+1` to serialize concurrent writers. `refresh_memory_summary`
rebuilds `memory_summary` from turns older than the window by **TRUNCATION, not an
LLM call**.

## Invariants & gotchas

- **turn_index integrity relies on the `append_turn` row lock — never insert
  `ChatMessage` rows directly.**
- `append_turn`'s optional `keywords` list is write-only (≤8 terms; `None` → `[]`);
  it does not re-filter.
- Consumers: server.py (/ask memory), `chats/routes`, `router-wiring`
  (`get_chat_session_for_user`, `list_recent_turns`), and `reports/ai-use-routes`
  (`get_chat_session_for_user`, `serialize_chat_session` — cross-domain).

## Env flags

`CHAT_MEMORY_WINDOW_TURNS`, `CHAT_MEMORY_SUMMARY_TRIGGER_TURNS`,
`CHAT_MEMORY_SUMMARY_MAX_CHARS`.

## Related

`chats/routes`, `chats/bundle-cache`, `rag-pipeline/router-wiring`,
`reports/ai-use-routes`, `database/models`.
