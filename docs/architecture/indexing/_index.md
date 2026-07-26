---
doc: indexing/_index
description: Router for the PDF-to-pgvector ingestion/indexing pipeline plus the pluggable OCR layer.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# indexing/ — ingestion & indexing

North-star: teacher/authored PDFs → layout `Item`s → chunks **1:1 with Items**
(never re-windowed, exact `page_number` preserved for citation markers) →
OpenAI `text-embedding-3-large` **3072-dim** embeddings (`EMBEDDING_DIM`) →
pgvector (`app.documents` doc-level + `internal.document_chunks`). No `aita_*`
legacy names survive anywhere in this code — the ORM is `Document`/
`DocumentChunk`/`DocumentStatus` (see [database/models](../database/models.md)).

| Leaf | One-liner | Owns |
|---|---|---|
| [indexing-service](indexing-service.md) | Legacy monolithic orchestrator (older API) | `indexing/indexing_service.py`, `__init__.py` |
| [checkpoint-indexer](checkpoint-indexer.md) | Production checkpointed embed/persist for long jobs | `indexing/checkpoint_indexer.py` |
| [connector-document](connector-document.md) | Input DTO + NUL sanitization at the boundary | `indexing/connector_document.py`, `text_sanitization.py` |
| [chunking-embedding](chunking-embedding.md) | Items→chunks + single/batched embedders | `indexing/document_chunker.py`, `document_embedder.py` |
| [persistence-hashing](persistence-hashing.md) | Dedup hashes + failure/attach write helpers | `indexing/document_hashing.py`, `document_persistence.py` |
| [ocr-core](ocr-core.md) | OCR provider contract + env-gated factory | `ocr/provider.py`, `factory.py`, `__init__.py` + README.md |
| [ocr-providers](ocr-providers.md) | Mathpix + OpenAI-vision concrete providers | `ocr/mathpix.py`, `openai_vision.py` |

## Cross-cutting invariants

- **Chunk = Item, 1:1.** No token windowing; `page_number`/`section_path`/
  `chunk_type` ride each chunk so citations stay page-exact.
- **No DB session held across an OpenAI call.** The checkpoint indexer's
  short-session pattern is the asyncpg conn-drop fix; `database/session.py`'s
  `pool_recycle` is only a backstop.
- **NUL bytes stripped, never rejected** at two chokepoints (connector DTO +
  chunker) because Postgres TEXT/JSONB reject `\x00`.

## External consumers (linked, not owned)

- [knowledge/teacher-weekly](../knowledge/teacher-weekly.md) — the weekly upload
  worker drives `prepare_for_indexing` + checkpoint `embed_and_persist_chunks`/
  `finalize_document`.
- [apollo/provisioning/authored-sets/indexing](../apollo/provisioning/authored-sets/indexing.md)
  — reuses the indexing core, overriding finalize to the hidden reference sentinel.
- [knowledge/teacher-pdf-ingestion](../knowledge/teacher-pdf-ingestion.md) —
  constructs Mathpix directly, bypassing `ocr/factory` (see ocr-core).
