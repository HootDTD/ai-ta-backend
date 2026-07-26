---
doc: indexing/chunking-embedding
description: The two stateless text-prep primitives — layout-aware chunker and the single/batched OpenAI embedders.
owns:
  - indexing/document_chunker.py
  - indexing/document_embedder.py
related:
  - indexing/connector-document
  - indexing/indexing-service
  - indexing/checkpoint-indexer
last_verified: 2026-07-25
stub: false
---

# chunking-embedding — chunker + embedders

Two stateless primitives feeding both indexers. No chonkie/ML deps — the
embedder calls OpenAI directly.

## Interface

- `items_to_chunk_texts(items) -> list[tuple[str, dict]]` — one layout `Item` →
  one chunk `(text, metadata)` pair **1:1, no token windowing** (preserves
  `page_number` for citations). Strips NUL from text + metadata, falls back to
  `item.raw_text` when `item.text` is empty, skips fully-empty items. Metadata:
  `page_number`, `section_path` (joined `" > "`), `chunk_type`, `figure_id`,
  `source_pdf`, `item_id`. Items are duck-typed via `getattr` (accepts a
  dataclass `Item` or `SimpleNamespace`).
- `embed_text(text, model=None, dim=None) -> list[float]` — single-text direct
  OpenAI call. Lazy thread-safe client singleton, LRU-cached via `_embed_cached`
  (`EMBED_CACHE_SIZE`, default 256), input truncated to 8000 chars. Used by the
  legacy `index_from_items`.
- `embed_texts(texts, model=None, dim=None) -> list[list[float]]` — batched
  (≤256 inputs/request, order-preserving via `item.index` sort). Used by
  `checkpoint_indexer`.

## Invariants & gotchas

- Both embedders default the model to `OPENAI_EMBEDDING_MODEL` or
  `text-embedding-3-large` and the dim to `EMBEDDING_DIM` or **3072**.
- `document_chunker` is the second `text_sanitization` chokepoint (the DTO is
  the first).

## Env flags

- `OPENAI_EMBEDDING_MODEL`, `EMBEDDING_DIM`, `EMBED_CACHE_SIZE`.

## Related

- [connector-document](connector-document.md) (shared `text_sanitization`),
  [indexing-service](indexing-service.md) (`embed_text`),
  [checkpoint-indexer](checkpoint-indexer.md) (`embed_texts`).
