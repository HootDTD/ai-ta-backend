---
doc: indexing/indexing-service
description: AITAIndexingService — the legacy monolithic indexing orchestrator (older API, single held session).
owns:
  - indexing/indexing_service.py
  - indexing/__init__.py
related:
  - indexing/chunking-embedding
  - indexing/persistence-hashing
  - indexing/connector-document
  - database/models
  - knowledge/teacher-weekly
last_verified: 2026-07-25
stub: false
---

# indexing-service — legacy orchestrator

`AITAIndexingService` is the older, monolithic ingestion API. Production
teacher-upload and authored-set flows now use
[checkpoint-indexer](checkpoint-indexer.md) instead; this service is retained
for `prepare_for_indexing` (still the dedup gate) and the simple synchronous
`index_from_items` path.

## Interface

- `AITAIndexingService(session: AsyncSession)`.
- `prepare_for_indexing([AITAConnectorDocument]) -> list[Document]` — persists
  new `Document` rows in `pending`, dedups on `unique_identifier_hash` +
  `content_hash`, re-queues content-changed docs, skips cross-source content
  duplicates. Commits once; on `IntegrityError` (concurrent-insert race) it
  rolls back and returns `[]`.
- `index_from_items(document, connector_doc, items) -> Document` — status
  `processing` → `items_to_chunk_texts` → `embed_text` **serially inside one
  held `AsyncSession`** (N API calls) → delete stale `DocumentChunk` rows for
  the doc → `attach_chunks_to_document` → status `ready`.

## Data flow

In: `AITAConnectorDocument` + layout `Item`s. Out: persisted `Document`
(`status` `ready`/`failed`) with attached chunks. Imports `Document`,
`DocumentChunk`, `DocumentStatus` from `database.models`.

## Invariants & gotchas

- **DRIFT (Appendix A #13):** imports the DB-07 names `Document`/`DocumentChunk`/
  `DocumentStatus` — NOT the legacy `AITADocument`/`AITAChunk` the old doc used.
  No `SearchSpace` — the course FK is `course_id`.
- **Swallow-everything failure path:** any exception in `index_from_items` routes
  to `rollback_and_persist_failure` (records `failed(message)`, never re-raises).
- Holds one session across the whole embed loop — the exact pattern the
  checkpoint indexer was built to avoid on long jobs.

## Related

- [chunking-embedding](chunking-embedding.md), [persistence-hashing](persistence-hashing.md),
  [connector-document](connector-document.md), [database/models](../database/models.md).
- [knowledge/teacher-weekly](../knowledge/teacher-weekly.md) calls
  `prepare_for_indexing`.
