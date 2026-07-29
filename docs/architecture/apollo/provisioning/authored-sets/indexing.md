---
doc: apollo/provisioning/authored-sets/indexing
description: Index authored problem/solution PDFs into HIDDEN (PENDING-status) AITA documents so they never reach student retrieval
owns:
  - apollo/provisioning/authored_sets/indexing.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/authored-sets/api
  - indexing/checkpoint-indexer
  - indexing/connector-document
  - indexing/indexing-service
  - indexing/chunking-embedding
  - indexing/persistence-hashing
  - database/models
  - database/session
last_verified: 2026-07-25
stub: false
---

## Interface

`index_authored_doc(db, *, search_space_id, file_bytes, title, set_index, role,
page_sink=None)` → the hidden `Document.id`. `role` must be `"problem"` or
`"solution"`. Called by `api._run_set_background`.

## Data flow

Write bytes to a temp PDF, run the real PyMuPDF/OCR ingest (offloaded via
`asyncio.to_thread` so a slow handwritten PDF never stalls the event loop serving
tutoring), build an `AITAConnectorDocument`, `prepare_for_indexing` →
`embed_and_persist_chunks` → `finalize_document`, then force the Document to
`DocumentStatus.PENDING` so it stays hidden from student retrieval. `page_sink`, when
supplied, receives the transient per-page OCR results (`NormalizedPage` objects) for
the caller's page-evidence write.

## Invariants & gotchas

- Reuses only the indexing CORE, not the weekly-upload wrapper: no `Upload` row, no
  supersede, no week activation, no generic Apollo provisioning-job dispatch.
- `_authored_doc_id` derives a deterministic id from `(search_space_id, set_index,
  role)`; `_find_existing_indexed_doc` matches by `unique_identifier_hash` then
  `content_hash` so a fresh-`set_index` re-upload of identical bytes reuses the
  already-indexed doc instead of failing when `prepare_for_indexing` dedups it away.
- The document row is committed before chunk batches persist (the checkpointed writer
  opens its own sessions and must see the row).
- `_run_ingest` (real PyMuPDF/OCR I/O) is behind a test seam and marked no-cover.

## Related

Cross-domain into the indexing domain: `checkpoint_indexer`
(`build_doc_content`/`embed_and_persist_chunks`/`finalize_document`),
`connector_document.AITAConnectorDocument`, `document_chunker.items_to_chunk_texts`,
`document_embedder.embed_text` (chunking-embedding),
`document_hashing` (persistence-hashing), `indexing_service.AITAIndexingService`,
`database.models.Document`/`DocumentStatus`, `database.session`.
