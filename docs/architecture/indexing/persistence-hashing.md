---
doc: indexing/persistence-hashing
description: Dedup hashing plus the low-level SQLAlchemy failure/attach write helpers shared by both indexers.
owns:
  - indexing/document_hashing.py
  - indexing/document_persistence.py
related:
  - indexing/indexing-service
  - indexing/checkpoint-indexer
  - database/models
last_verified: 2026-07-25
stub: false
---

# persistence-hashing — dedup hashes + write helpers

Two small utils, both consumed by the indexers.

## Interface

- `compute_unique_identifier_hash(doc) -> str` — SHA-256 of
  `"{document_type}:{unique_id}:{search_space_id}"` (identity dedup;
  same document, different course → different hash).
- `compute_content_hash(doc) -> str` — SHA-256 of
  `"{search_space_id}:{source_markdown}"` (revision detection).
- `rollback_and_persist_failure(session, document, message)` — rolls back, then
  best-effort records `Document.failed(message)` + `failure_reason`.
- `attach_chunks_to_document(document, chunks)` — attaches the `chunks`
  relationship via `set_committed_value`, then sets `document_id`/`course_id` and
  `add_all`s them when the doc is persisted.

## Invariants & gotchas

- **`rollback_and_persist_failure` must NEVER raise** — it is called only from
  `except` blocks, so a raise there would mask the original indexing exception.
  Both its `rollback` and its status write are wrapped in bare `except`.
- **`attach_chunks_to_document` uses `set_committed_value`** to attach the
  relationship *without* triggering async lazy-loading (avoids `MissingGreenlet`).
- Forced reindex works by appending an `<!-- reindex:{marker} -->` comment to
  `source_markdown` upstream, which busts `compute_content_hash`.

## Related

- [indexing-service](indexing-service.md), [checkpoint-indexer](checkpoint-indexer.md)
  (both callers), [database/models](../database/models.md).
