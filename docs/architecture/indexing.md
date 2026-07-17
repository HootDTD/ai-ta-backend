---
doc: ai-ta-backend/indexing
description: Document ingestion pipeline — layout-aware PDF extraction, OpenAI embeddings, OCR fallbacks, and pgvector persistence for course materials.
owns:
  - indexing/**
  - ocr/**
related:
  - ai-ta-backend/rag-pipeline
  - shared/supabase
last_verified: 2026-07-16
stub: false
---

# Indexing

How course material PDFs become searchable vectors. Ported from SurfSense's indexing pipeline with educational adaptations (material kinds, weekly gating, citation-grade page tracking). The pipeline's defining design choice: chunks are **1:1 with layout-extracted Items**, never re-windowed, so every chunk keeps its exact page number for citation markers like "[Textbook, p. 42]".

## Module map and file landmarks

### indexing/ — the pgvector ingestion pipeline (8 files)

| File | Role |
|---|---|
| `indexing/__init__.py` | Package marker, docstring only. |
| `indexing/connector_document.py` | `AITAConnectorDocument` — Pydantic DTO entering the pipeline. Fields: `title`, `source_markdown`, `unique_id`, `document_type` (default `"EDUCATIONAL_FILE"`), `search_space_id` (the course), `material_kind`, `page_count`, `week` (None = permanent material), `metadata` dict. `VALID_MATERIAL_KINDS = {textbook, slides, homework, exams, notes, other}`. Validators strip NUL bytes from `title`/`source_markdown`/`unique_id` and recursively from `metadata` (see `text_sanitization.py`). |
| `indexing/document_chunker.py` | `items_to_chunk_texts(items)` — converts layout Items to `(text, metadata)` pairs, 1:1. Strips NUL bytes from chunk text and metadata strings. Falls back to `item.raw_text` when `item.text` is empty; skips fully empty items. Metadata carries `page_number`, `section_path` (joined with `" > "`), `chunk_type`, `figure_id`, `source_pdf`, `item_id`. Items are duck-typed via `getattr` — accepts dataclass `Item` or `SimpleNamespace`. |
| `indexing/text_sanitization.py` | `strip_nul(text)` + `sanitize_jsonable(value)` — remove `\x00` from strings (recursively for JSON-able structures). Postgres TEXT/JSONB reject NUL; PyMuPDF/Mathpix extraction can emit it (a 873-page scanned textbook killed the `aita_documents` INSERT on staging). Applied at the two chokepoints: `AITAConnectorDocument` validators and `items_to_chunk_texts`. |
| `indexing/document_embedder.py` | `embed_text(text, model=None, dim=None)` — direct OpenAI call (no chonkie/ML deps). Defaults: `OPENAI_EMBEDDING_MODEL` env or `text-embedding-3-large`, `EMBEDDING_DIM` env or 3072. LRU-cached (`EMBED_CACHE_SIZE`, default 256). Truncates input to 8000 chars. Thread-safe lazy OpenAI client singleton. `embed_texts(texts, model=None, dim=None)` — batched variant: issues one OpenAI request per ~256 texts (configurable); used by the checkpointed indexer to minimise API round-trips. |
| `indexing/checkpoint_indexer.py` | Checkpointed embed-and-persist logic for long-running textbook indexing jobs. Key functions: `group_pages(chunks)` groups `(text, metadata)` pairs by page number; `plan_batches(pages, resume_page)` packs whole pages into chunk-count-bounded batches, skipping pages already committed (resume pointer); `embed_and_persist_chunks(batches, doc, session_factory, ...)` embeds each batch with `embed_texts`, then persists in its **own short-lived `AsyncSession`** — the DB session is never held open while embeddings are in flight — advancing `artifact_manifest.embed_progress.last_completed_page` after each commit and renewing the job lease; `build_doc_content(chunks)` assembles the document-level body text; `finalize_document(doc, connector_doc, session_factory, ...)` terminal write in a fresh short session — persists null-page chunks, doc-level content/embedding, and marks the doc `READY`. |
| `indexing/document_hashing.py` | `compute_unique_identifier_hash` = SHA-256 of `"{document_type}:{unique_id}:{search_space_id}"` (identity dedup). `compute_content_hash` = SHA-256 of `"{search_space_id}:{source_markdown}"` (revision detection). |
| `indexing/document_persistence.py` | `rollback_and_persist_failure` (called only from except blocks; must never raise) and `attach_chunks_to_document` (uses `set_committed_value` to attach chunks without triggering SQLAlchemy async lazy loading). |
| `indexing/indexing_service.py` | `AITAIndexingService` — the orchestrator. See Public interfaces. |

### ocr/ — pluggable OCR provider layer (5 files)

| File | Role |
|---|---|
| `ocr/provider.py` | Abstractions: `OCRBlock` (kind `'text'`/`'latex'`, text, confidence), `OCRResult` (`.fused_text` joins blocks with `\n\n`; `.average_confidence`), `OCRProvider` ABC with `recognize(image_bytes, mime, dpi) -> OCRResult`. |
| `ocr/mathpix.py` | `MathpixOCRProvider` — POSTs base64 data-URL JSON to `https://api.mathpix.com/v3/text` via stdlib `urllib` (no requests). Requests formats `["text", "latex_styled"]`; parses plain-text and LaTeX into separate blocks. `config_from_env()` reads `MATHPIX_APP_ID`, `MATHPIX_APP_KEY`, `MATHPIX_ENDPOINT`, `OCR_DPI`. |
| `ocr/openai_vision.py` | `OpenAIVisionOCRProvider` — OpenAI Chat Completions vision OCR adapter for rendered page images. Sends the page as a base64 data URL, requests JSON `{text, confidence}`, returns one LaTeX-flavored `OCRBlock`, and fails soft to an empty `OCRResult` on malformed/provider errors. `from_env()` reads `APOLLO_OCR_MODEL` (default `gpt-4o`) and reuses `OPENAI_API_KEY` through the OpenAI SDK. |
| `ocr/factory.py` | `get_ocr_provider_from_env()` — returns Mathpix when `OCR_PROVIDER=mathpix` and credentials exist, OpenAI vision when `OCR_PROVIDER=openai`, else None. **Not wired into the existing weekly upload runtime flow** (per `ocr/README.md`, intentional). The live weekly consumer still constructs Mathpix directly; authored-set indexing will pass a factory-selected provider — see below. |
| `ocr/README.md` | Env flags + usage snippet. |

### Adjacent files this doc references but does not own

- `knowledge/teacher_pdf_ingestion.py` — `TeacherPDFIngestor`, the **production** layout-aware PDF extractor (PyMuPDF native + selective Mathpix). Builds its own provider via `build_teacher_mathpix_provider()`, bypassing `ocr/factory.py`.
- `apollo/provisioning/authored_sets/indexing.py` — authored-set PDF indexer. It passes the env-selected `OCRProvider` into `TeacherPDFIngestor`, then reuses `AITAIndexingService.prepare_for_indexing`, `embed_and_persist_chunks`, `build_doc_content`, and `finalize_document`. It skips the weekly upload wrapper and overrides `finalize_document`'s ready status to the hidden sentinel `{"state": "apollo_reference"}` so authored problem/solution PDFs are not visible to student RAG. The connector metadata carries per-page `page_debug` (`page`, `ocr_confidence`, `extraction_mode`) — the same shape the weekly DTO uses — so the authored-set verification path (`chunk_ocr_confidence`) can detect low-confidence (e.g. handwritten) pages; without it the low-OCR generate-and-compare cross-check never fires. The synchronous PyMuPDF + per-page OCR ingest runs via `asyncio.to_thread` so a multi-page handwritten PDF does not stall the event loop serving concurrent requests.
- `knowledge/teacher_weekly.py` — upload queue, job leasing, worker loop, week activation. Calls `AITAIndexingService`.
- `knowledge/manager.py` — legacy `add_pdf_material()` path + `_index_items_to_pgvector()` bridge.
- `text-embeder/layout_multimodal_embedder.py` — original CLI extractor/embedder (FAISS + SQLite FTS5); still used by `knowledge/manager.py`.
- `teacher_upload_worker.py` — 20-line worker entrypoint (Procfile `worker:` process); just runs `TeacherWeeklyStorage().run_upload_worker_loop()`.

## Public interfaces

```python
# indexing/indexing_service.py
service = AITAIndexingService(session)                       # AsyncSession
docs = await service.prepare_for_indexing([connector_doc])   # -> list[AITADocument] needing (re-)index
doc  = await service.index_from_items(docs[0], connector_doc, items)  # -> AITADocument (status ready/failed)
```

- `prepare_for_indexing` persists new `AITADocument` rows in `pending` status, skips unchanged docs (same identity hash + content hash), re-queues content-changed docs, skips cross-source content duplicates, and handles concurrent-insert races by rolling back on `IntegrityError` (returns `[]`).
- `index_from_items` sets status `processing` → chunks → embeds → deletes stale `AITAChunk` rows for the doc id → attaches new chunks → status `ready`. On any exception: `rollback_and_persist_failure` records `failed(message)` and never re-raises.
- `embed_text(text) -> list[float]` (indexing/document_embedder.py) — single-text embedding; used by `index_from_items` (legacy monolithic path) and as a fallback.
- `embed_texts(texts) -> list[list[float]]` (indexing/document_embedder.py) — batched embedding (~256 texts/request); used by the checkpointed indexer path.
- `items_to_chunk_texts(items) -> list[tuple[str, dict]]` (indexing/document_chunker.py).
- `from ocr import get_ocr_provider_from_env, OCRProvider, OCRResult, OCRBlock`.
- `apollo.provisioning.authored_sets.indexing.index_authored_doc(db, *, search_space_id, file_bytes, title, set_index, role) -> int` — Apollo-authored problem/solution set entry point that consumes the indexing core and returns the hidden `AITADocument.id`.

## Main data flows

### Flow 1 — Teacher upload → pgvector (production path)

1. **Upload**: `server.py:upload_teacher_material` (`POST /teacher/upload`, returns 202) — validates teacher course membership + `.pdf` suffix, streams to temp file, calls `knowledge/teacher_weekly.py:TeacherWeeklyStorage.enqueue_upload_by_search_space`.
2. **Enqueue**: `teacher_weekly.py:_enqueue_upload_by_search_space_async` — SHA-256s the file, uploads PDF bytes to Supabase Storage (upload bucket), inserts `teacher_uploads` row (status `queued`) + `teacher_upload_jobs` row. The HTTP request ends here; everything else is async.
3. **Worker claim**: `teacher_upload_worker.py:main` → `teacher_weekly.py:run_upload_worker_loop` → `process_next_upload_job` → `_claim_next_upload_job_async` (lease-based job claiming, worker id = `host:pid:uuid8`). `_process_claimed_upload_job` downloads the PDF from storage into a temp dir.
4. **Extraction**: `teacher_weekly.py:_ingest_pdf_upload` → `knowledge/teacher_pdf_ingestion.py:TeacherPDFIngestor.ingest`. Per page:
   - `_extract_native_page` — PyMuPDF `page.get_text("dict")` blocks; classifies `heading` (avg font ≥ 14pt and ≤ 140 chars), `equation` (math-symbol ratio ≥ 0.12), else `body`.
   - `choose_mathpix_strategy` — flags page for Mathpix when: no/low native text (< 120 chars, `TEACHER_MATHPIX_MIN_TEXT_CHARS`), image-dominant (image area ratio ≥ 0.45), or equation/handwriting-heavy (math ratio ≥ 0.08, or ≥ 12 vector drawings with little text).
   - Page rendered to PNG at 300 DPI (`TEACHER_UPLOAD_RENDER_DPI`) and stored to the pages bucket via `teacher_weekly.py:_store_page_asset` (key `teacher-uploads/{upload_id}/page-NNNN.png`).
   - `ocr/mathpix.py:MathpixOCRProvider.recognize` on flagged pages; `merge_page_models` fuses native + OCR text — Mathpix output with average confidence < 0.4 (`TEACHER_MIN_OCR_CONFIDENCE`) is **discarded** (`extraction_mode="native_ocr_rejected"`); duplicates between native and OCR text removed by character-trigram Jaccard ≥ 0.75 (`TEACHER_FUZZY_DEDUPE_THRESHOLD`).
   - `_page_to_items` — one `SimpleNamespace` Item per surviving region, id `{doc_id}:{page}:{region_index}`; LaTeX regions become `chunk_type="equation"` with text `"{page_plain_text}\n\nLaTeX:\n{latex}"`.
5. **DTO build**: `_ingest_pdf_upload` wraps everything in an `AITAConnectorDocument` (`unique_id=f"teacher-upload:{upload_id}"`, `source_markdown` = joined per-page plain text, rich `metadata` incl. `ocr_summary`, `artifact_manifest`, per-page `page_debug`, `ocr_degraded` flag).
6. **Chunk + embed (checkpointed path)**: `teacher_weekly.py:_index_existing_upload_async` now runs three short-session phases — (a) document upsert + read resume pointer from `artifact_manifest.embed_progress.last_completed_page`; (b) `indexing/checkpoint_indexer.py:embed_and_persist_chunks` — batched embedding via `embed_texts` (~256 texts per OpenAI call), per-page commits in isolated sessions (no session held across embedding), with resume-pointer advance + job-lease renewal after each batch; (c) `indexing/checkpoint_indexer.py:finalize_document` — writes null-page chunks + doc-level content/embedding and marks the doc `READY`. The doc-level content is assembled by `build_doc_content` over the first 2000 chars of body/heading/ocr text.
7. **Storage**: vectors land in **pgvector** — `aita_documents.embedding` and `aita_chunks.embedding`, both `Vector(EMBEDDING_DIM)` = `Vector(3072)` (`database/models.py:125-180`). Chunks carry `page_number`, `section_path`, `chunk_type`, `figure_id`. Finally `teacher_weekly.py:_sync_week_activation` flips document statuses: only the latest ready upload per (week, kind) with `week <= current_week` stays `ready`; the rest go inactive. No FAISS in this path.
8. **Finalize upload**: after the document reaches `READY`, the upload job and
   week activation are committed. Cleanup T-F removed the dormant Apollo enqueue
   seam, so a normal teacher upload creates no provisioning job or extra ingest
   run. Authored sets use their separate synchronous API and observability path.

### Flow 2 — Legacy KnowledgeManager path (`knowledge/manager.py:add_pdf_material`)

`text-embeder/layout_multimodal_embedder.py:extract_document` (PyMuPDF blocks, 1000-token chunks with 150-token overlap via tiktoken `cl100k_base`, numbered-heading section tracking, repeating header/footer suppression) → `embed_items` (batched 64, exponential-backoff retries, L2-normalized float32) → `np.save embeddings.npy` + `build_faiss` (FAISS `IndexHNSWFlat(dim, 64)`, efConstruction 200, efSearch 128 → `faiss.index`) + `build_sqlite` (`items_raw` table + FTS5 `items` virtual table, porter tokenizer → `sqlite.db`), all under `km_<uuid>/` per material. Then `manager.py:_index_items_to_pgvector` dual-writes the same Items through `AITAIndexingService` (comment in code: "the only write path now" — pgvector is authoritative; FAISS/SQLite artifacts are legacy local-store outputs).

### OCR fallback ladder (three distinct mechanisms)

1. **Mathpix selective per-page** (teacher path, Flow 1 step 4) — production; constructed directly from `MATHPIX_APP_ID`/`MATHPIX_APP_KEY` env.
2. **Tesseract whole-page** (`layout_multimodal_embedder.py`, legacy path) — only when a page yields < 500 native chars; renders at 300 DPI, OCR lines re-chunked at the same token limits, item ids `{doc_id}:{page}:oN`.
3. **`ocr/factory.py` env-gated provider** (`OCR_PROVIDER=mathpix` or `OCR_PROVIDER=openai`) — exists for authored-set indexing and tests; the weekly upload wrapper still constructs Mathpix directly. `knowledge/teacher_pdf_ingestion.py` accepts any `OCRProvider` at the existing `mathpix_provider` parameter name and only calls `.recognize()`.

## Key dependencies

- **PyMuPDF (`fitz`)** — required for extraction; `TeacherPDFIngestor.ingest` raises `RuntimeError` without it. Optional in the legacy embedder (degrades for tests).
- **OpenAI SDK** — embeddings (`text-embedding-3-large`). Lazy-imported in both embedder modules.
- **pgvector.sqlalchemy `Vector`** — column type on `aita_documents` / `aita_chunks`.
- **SQLAlchemy async + asyncpg** — `AITAIndexingService` takes an `AsyncSession`; worker bridges sync→async via `database/session.py:run_async`.
- **Pydantic** — `AITAConnectorDocument`, OCR models.
- **Mathpix HTTP API** — via stdlib `urllib.request` only.
- Optional (legacy embedder only): `faiss`, `pytesseract`+`PIL`, `cv2`, `tiktoken`, `numpy`, `transformers` (BLIP figure captioning).

## Non-obvious conventions

- **Chunk sizing differs by path**: teacher path does NO token windowing (1 layout Item = 1 chunk, preserving exact pages for citations); legacy embedder windows at 1000 tokens / 150 overlap (`KNOWLEDGE_TOKEN_LIMIT` / `KNOWLEDGE_OVERLAP_TOKENS`).
- **Embedding model/dims**: `text-embedding-3-large`, 3072 dims everywhere (`EMBEDDING_DIM` env, `database/models.py:25`). The checkpointed teacher-upload path uses `embed_texts` (~256 texts per API call, no session held during the call); the older `index_from_items` path in `indexing_service.py` still embeds one chunk at a time (N API calls per doc) with the 256-entry LRU cache. `embed_text` truncates at 8000 chars; doc-level content capped at 2000 chars.
- **`index_from_items` is a known follow-up**: `indexing/indexing_service.py:index_from_items` retains the older monolithic pattern (serial per-chunk embedding inside one held `AsyncSession`) because `knowledge/manager.py` still calls it. That path was not changed in the checkpointed-indexing fix — migrating it is a deferred follow-up.
- **Dedup/reindex semantics**: identical `unique_identifier_hash` + `content_hash` → skipped; content change → re-index in place. Forced reindex works by appending `<!-- reindex:{marker} -->` to `source_markdown` (`teacher_weekly.py:_ingest_pdf_upload`), changing the content hash.
- **`material_kind` is silently coerced**: invalid kinds become `"other"` (validator in `connector_document.py`), not rejected. It drives retrieval store-bias weights downstream.
- **NUL bytes are stripped, never rejected**: extraction output legitimately contains `\x00` on scanned PDFs; the pipeline silently removes it at the DTO and chunker boundaries (regression test: `tests/database/test_indexing_nul_postgres.py` on real Postgres — SQLite cannot catch this).
- **Mathpix rejection is silent-but-flagged**: confidence < 0.4 discards OCR text; pages that needed Mathpix but didn't get it set `metadata.ocr_degraded=True` — indexing still proceeds with whatever native text exists.
- **Failure handling**: `rollback_and_persist_failure` is deliberately swallow-everything (a raise there would mask the original indexing exception); a failed doc is retried on next upload.
- **`attach_chunks_to_document` uses `set_committed_value`** — required to avoid async lazy-load (`MissingGreenlet`) when assigning the `chunks` relationship.
- **Worker process**: Procfile defines `worker: python -m teacher_upload_worker`; jobs are claimed with lease ownership so multiple workers are safe (IntegrityError race handling in `prepare_for_indexing` covers the document table).

## Product context

Hoot is a citation-backed teaching assistant: students ask questions, answers must cite course material by source and page. This pipeline exists so teachers can drop weekly PDFs (slides, homework, exams...) into a course and have them retrievable minutes later — with week gating (`week` > current week stays inactive so students can't see future material) and per-kind retrieval weighting. The 1:1 Item→chunk rule and the Mathpix path for equation/handwriting-heavy pages both serve the same product requirement: citations must point at real pages, and math content must survive extraction. Per repo `CLAUDE.md`: pgvector + FAISS are the only sanctioned vector stores, and citation marker generation is non-negotiable.
