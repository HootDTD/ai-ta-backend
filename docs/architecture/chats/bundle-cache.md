---
doc: chats/bundle-cache
description: Session-scoped retrieval bundle cache backing the NONE/AUGMENT retrieval-mode router.
owns:
  - chats/bundle_cache.py
related:
  - rag-pipeline/router-wiring
  - rag-pipeline/router-mode
  - rag-pipeline/document-visibility
  - chats/service
  - platform/config-contracts
  - database/models
last_verified: 2026-07-25
stub: false
---

# chats/bundle-cache — the retrieval bundle cache

Persists the snippets (and citation-scoring rows) from the most recent retrieval
so NONE/AUGMENT turns can skip pgvector + the scoring wave. Consumed only by
`ai/router/wiring` (`router-wiring`).

## Interface

- `@dataclass(frozen=True) CachedBundle` (`snippets`, `scoring`,
  `visible_docs_hash`, `saved_turn`; `.titles` property).
- `visible_docs_fingerprint(doc_ids) -> str` and
  `async compute_visible_docs_hash(db_session, search_space_id) -> str` — fingerprint
  the searchable doc set via `active_document_conditions`
  (`rag-pipeline/document-visibility`).
- `async load_bundle_cache(...)` / `save_bundle_cache(...)` — persist BundleSnippets
  + scoring rows into `chat_session_snippets` (`snippet_payload` JSONB =
  `{"snippet": asdict, "scoring": row}`), LRU-evicting beyond
  `BUNDLE_CACHE_MAX_CHUNKS`.

## Data flow

The visible-docs fingerprint is stored under `ChatSession.metadata_["bundle_cache"]`
(`_META_KEY`). **Staleness contract**: on a fingerprint mismatch, callers
(`router-wiring`) must route FRESH. `save_bundle_cache` with `replace=True`
(FRESH) drops the prior cache; `replace=False` (NONE/AUGMENT) merges.

## Invariants & gotchas

- **Fail-toward-no-cache**: a corrupt or contract-drifted `snippet_payload`
  (fails `BundleSnippet(**...)` / `.validate()`) degrades to a cache MISS (FRESH),
  never an error.
- Reads `ChatSession` / `ChatSessionSnippet` / `Document` (`database/models`).

## Env flags

`BUNDLE_CACHE_MAX_CHUNKS`.

## Related

`rag-pipeline/router-wiring`, `rag-pipeline/router-mode`,
`rag-pipeline/document-visibility`, `chats/service`.
