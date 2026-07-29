---
doc: indexing/checkpoint-indexer
description: Checkpointed embed-and-persist for long textbook jobs — the production indexing path, never holds a session across an OpenAI call.
owns:
  - indexing/checkpoint_indexer.py
related:
  - indexing/chunking-embedding
  - database/models
  - database/session
  - knowledge/teacher-weekly
  - apollo/provisioning/authored-sets/indexing
last_verified: 2026-07-25
stub: false
---

# checkpoint-indexer — production checkpointed path

Splits a document's chunks into per-page work units, embeds each page-batch in
one batched OpenAI call, and commits each batch in its **own short-lived DB
session** while advancing a resume pointer. This is the fix for the
connection-reap failure on long jobs.

## Interface

- `PageGroup(page_number, items)` dataclass.
- `group_pages(chunk_pairs) -> (list[PageGroup], null_page_pairs)` — groups
  `(text, metadata)` by `page_number`; `None`-page items are returned separately
  (persisted once at finalize, not resumable units).
- `plan_batches(page_groups, *, batch_size, after_page) -> Iterator[list[PageGroup]]`
  — packs **whole** pages into chunk-count-bounded batches, skipping pages
  `<= after_page` (resume). A single oversized page is its own batch (never split
  — the page is the commit/idempotency unit).
- `embed_and_persist_chunks(*, session_factory, document_id, chunk_pairs, after_page,
  batch_size, on_progress, embed_fn) -> int` — per batch: embed via `embed_texts`,
  then in a fresh `session_factory()` session delete-and-reinsert that page's
  chunks and commit. Returns highest page committed; `on_progress(page)` may be
  sync or async. `embed_fn` defaults to `embed_texts`, resolved at call time so
  tests monkeypatch `checkpoint_indexer.embed_texts`.
- `build_doc_content(chunk_pairs, *, fallback_title) -> str` — doc-level body
  (body/heading/ocr text, first ~2000 chars).
- `finalize_document(session, *, document_id, chunk_pairs, doc_content,
  doc_embedding, page_count, embed_fn)` — terminal write in the caller's session:
  embeds null-page texts in one call, persists them + doc-level fields, marks
  `READY`. **Does NOT commit** — composes into the caller's finalize transaction.

## Invariants & gotchas

- **Load-bearing: no DB session is ever held while an OpenAI call is in flight.**
  Every embed happens before the `async with session_factory()` block opens.
- Re-running a page deletes then reinserts that page's chunks — idempotent.
- Resume pointer lives in `Upload.artifact_manifest.embed_progress.last_completed_page`
  (advanced by the `knowledge/teacher-weekly` caller after each batch commits).

## Env flags

- `EMBED_BATCH_SIZE` (default 128) — chunks per page-batch.

## Related

- [chunking-embedding](chunking-embedding.md) (`embed_texts`),
  [database/session](../database/session.md) (short-session pattern; its
  `pool_recycle` is only a backstop), [database/models](../database/models.md).
- Consumers: [knowledge/teacher-weekly](../knowledge/teacher-weekly.md) (three
  short-session phases) and
  [apollo/provisioning/authored-sets/indexing](../apollo/provisioning/authored-sets/indexing.md).
