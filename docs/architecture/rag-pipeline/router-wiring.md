---
doc: rag-pipeline/router-wiring
description: Server glue binding the retrieval-mode router to the /ask pipeline and the session bundle cache.
owns:
  - ai/router/wiring.py
  - ai/router/__init__.py
related:
  - rag-pipeline/router-mode
  - rag-pipeline/router-llm
  - rag-pipeline/retrieve-pipeline
  - chats/bundle-cache
  - chats/service
  - platform/config-contracts
  - database/models
last_verified: 2026-07-25
stub: false
---

# router-wiring — the integration hub

The integration hub of the always-on router. server.py imports it as
`from ai.router import wiring`. `ai/router/__init__.py` is an EMPTY namespace
package (not a re-export facade). **Cross-domain**: depends on `chats.bundle_cache`
and `chats.service`.

## Interface

All imported by server.py:
- `async prepare_router_context(*, chat_id, user_id, search_space_id, question,
  has_attachments) -> RouterTurnContext | None` — loads the cache, invalidates on
  visible-docs fingerprint change, runs `decide_retrieval_mode`. Attachments force
  FRESH without an LLM call. Returns None → callers take the legacy FRESH path.
- `bundle_from_cache(...)` (NONE: rebuild a `ResearchBundle` from cache).
- `merge_augment_bundle(...)` (AUGMENT: fresh-first, dedupe by id, cap at
  `ROUTER_MAX_SNIPPETS`).
- `persist_turn_outcome(...)` (save bundle + scoring rows to cache and write a
  `ChatRouterDecision` telemetry row).
- `scoring_rows_from_bundle`, `_assemble_bundle`, `_get_llm_router` (cached LLMRouter).
- `CACHED_SCORES_KEY = "cached_citation_scores"` provenance key lets fully-cached
  bundles skip the scorer wave.

## Data flow

`prepare_router_context` runs after the user turn is persisted → cache load +
visible-hash check + `list_recent_turns` (`chats/service`) → mode decision. server
picks the path (NONE/AUGMENT/FRESH). `persist_turn_outcome` saves the bundle back
(FRESH replaces, NONE/AUGMENT merge).

## Invariants & gotchas

- **Uses `get_async_session` directly (NOT `get_db_session`)** — reached only via
  the sync `/ask` bridge, so it is never RLS-enforced.
- **`persist_turn_outcome` never raises** — cache/telemetry failures must not
  break the request.

## Env flags

`ROUTER_AUGMENT_TOP_K`, `ROUTER_AUGMENT_TOKEN_BUDGET`, `ROUTER_MAX_SNIPPETS`
(falls back to `K_SEM`), `ROUTER_RECENT_TURNS`.

## Related

`router-mode`, `router-llm`, `retrieve-pipeline`, `chats/bundle-cache`,
`chats/service`.
