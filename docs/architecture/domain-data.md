---
doc: ai-ta-backend/domain-data
description: SQLAlchemy async models and per-loop session management, chat session/turn persistence with memory summarization, knowledge store CRUD, and AI-use PDF report generation
owns:
  - database/**
  - chats/**
  - knowledge/**
  - reports/**
related:
  - shared/supabase
  - shared/security
  - ai-ta-backend/rag-pipeline
last_verified: 2026-06-11
stub: false
---

## Module map and file landmarks

| Path | Role |
|------|------|
| `database/models.py` | All SQLAlchemy ORM models (12 tables, pgvector `Vector(3072)` columns) plus `DocumentStatus` JSONB-state helper |
| `database/session.py` | Async engine/session factory keyed **per event loop**, plus `run_async()` sync-to-async bridge over a daemon-thread loop |
| `database/migrations/` | `001`–`003` are one-time Python scripts (schema create, Supabase REST seed, FAISS re-embed); `004`–`024` are raw SQL files applied manually / via Supabase MCP (NOT Alembic); `023_chunks_halfvec_hnsw.sql` codifies the HNSW expression index on `aita_chunks.embedding::halfvec(3072)` (idempotent `IF NOT EXISTS`); **`023` is duplicated** (`023_apollo_auth_scoping.sql` exists too — renumber the next collision); `024_teacher_textbook.sql` relaxes the `teacher_uploads` week/kind checks for course-wide textbooks (`week=0`, `kind='textbook'`) and **must be applied before the textbook backend code deploys**. The ORM declares no CHECK constraints, so `tests/database/test_teacher_uploads_constraints.py` applies the real migration SQL on Testcontainers Postgres to guard them |
| `chats/service.py` | Chat session/turn CRUD primitives + memory summarization (`build_memory_context`, `append_turn`, `refresh_memory_summary`, `serialize_chat_session`) |
| `chats/routes.py` | `APIRouter` for `/chats` list/get/upsert/delete (sync endpoints bridging via `run_async`) |
| `knowledge/manager.py` | `KnowledgeManager` — subject/store CRUD over `aita_search_spaces`/`aita_documents`, legacy FAISS index dirs under `knowledge/text-embeder/knowledge/`, and pgvector dual-write `_index_items_to_pgvector()` |
| `knowledge/db.py` | `DBKnowledgeManager` — pgvector-only mirror of the manager interface; defined but currently has **no importers** outside its own file |
| `knowledge/teacher_weekly.py` | `TeacherWeeklyStorage` — teacher weekly uploads (Supabase Storage + DB job queue with leases), current-week + retrieval-weight controls, week-based document activation. `list_course_by_search_space` returns the per-week notes/slides grid plus a course-level `textbook` section (course-wide material, `_assemble_course_payload`) |
| `knowledge/teacher_pdf_ingestion.py` | `TeacherPDFIngestor` — PyMuPDF native extraction with per-page heuristic Mathpix OCR fallback, fuzzy trigram dedupe, page→chunk items |
| `reports/ai_use/models.py` | Pydantic `AIUseReport` schema + Supabase **REST** CRUD on `ai_use_reports` (via `vendors.supabase_client`, not SQLAlchemy) |
| `reports/ai_use/service.py` | Evidence pack assembly (redaction, prompt hashing, tool-call dedupe, token budget) + `generate_report()` LLM call |
| `reports/ai_use/pdf.py` | Markdown → HTML (python-markdown) → PDF (WeasyPrint) with print CSS |
| `reports/ai_use/routes.py` | `APIRouter` for `/reports/ai-use` create/get/list/PDF with chat-ownership checks |

Worker entrypoint: `teacher_upload_worker.py` (repo root) runs `TeacherWeeklyStorage().run_upload_worker_loop()` — the Procfile worker process.

## Public interfaces

### Models (`database/models.py`) — key columns
- `SearchSpace` (`aita_search_spaces`): `name`, `slug` (unique), `subject_name`, `weight_overrides` JSONB, `metadata`. One row per course.
- `AITADocument` (`aita_documents`): `title`, `material_kind`, `content`, `source_markdown`, `content_hash` (unique), `unique_identifier_hash` (unique), `embedding Vector(EMBEDDING_DIM=3072)`, `document_metadata` JSONB, `week`, `status` JSONB (`{"state": ready|pending|processing|failed|inactive}`), FK `search_space_id` CASCADE.
- `AITAChunk` (`aita_chunks`): `content`, `embedding Vector(3072)`, `page_number`, `section_path`, `chunk_type` (body/heading/equation), `figure_id`, FK `document_id` CASCADE.
- `CourseMembership` (`course_memberships`): composite PK (`user_id` UUID, `search_space_id`), `role` (student/teacher).
- `CourseInviteLink` (`course_invite_links`): `code` (unique), `role`, `created_by`, `is_active`, `max_uses`, `use_count`, `expires_at`.
- `TeacherCourse` (`teacher_courses`): unique `search_space_id`, `current_week` (default 1), `weights` JSONB, `weight_bounds` JSONB.
- `TeacherUpload` (`teacher_uploads`): `week`, `kind` (notes/slides/textbook), `status` (queued/processing/ready/failed/superseded), `storage_key`, `doc_id` FK → aita_documents SET NULL, `is_latest`, `ocr_provider`, `ocr_summary` JSONB, `artifact_manifest` JSONB, `metadata` JSONB. **Course-wide vs weekly kinds:** `notes`/`slides` are weekly (`WEEKLY_KINDS`); `textbook` is course-wide (`COURSE_WIDE_KINDS`). A textbook's tracking row uses the `week=0` sentinel (`COURSE_WIDE_WEEK`) — which reuses the `(search_space_id, week, kind)` supersede/replace logic unchanged — while its indexed `AITADocument` stores `week=NULL` so it stays visible for every week (the weekly activate/deactivate cycle only touches rows where `week IS NOT NULL`). This mirrors the legacy-migrated prod textbook (`material_kind='textbook'`, `week=NULL`).
- `TeacherUploadJob` (`teacher_upload_jobs`): durable work queue — `state` (queued/processing/completed/failed), `lease_owner`, `lease_expires_at`, `attempt_count`, `last_error`.
- `ChatSession` (`chat_sessions`): `chat_id` (string, unique, client-supplied), `user_id` UUID, `search_space_id` FK, `meta` JSONB, `memory_summary` TEXT, `topic_centroid_vector Vector(3072)`.
- `ChatTurn` (`chat_turns`): `chat_session_id` FK, `turn_index`, `turn_id`, `role`, `content`, `model`, `tool_name`, `tool_inputs` JSONB, `attachments` JSONB, `citations` JSONB.
- `ChatSessionSnippet` (`chat_session_snippets`): composite PK (`chat_session_id`, `chunk_id`), `original_score`, `first_seen_turn`, `last_used_turn`, `snippet_payload` JSONB — citation cache enabling retrieval-mode NONE/AUGMENT to skip pgvector.
- `ChatRouterDecision` (`chat_router_decisions`): per-`/ask` telemetry — stage1/stage2 router scores, `retrieval_mode`, `final_route`, clarify flags, per-stage latencies.

`DocumentStatus` is a static-method helper for the JSONB status dicts (`ready()`, `failed(reason)`, `is_state()`, `get_failure_reason()`).

### Session management (`database/session.py`)
- `get_db_session()` — FastAPI dependency yielding `AsyncSession`.
- `get_async_session()` — async context manager for non-FastAPI callers.
- `run_async(coro)` — runs a coroutine on a persistent daemon-thread event loop and blocks for the result; this is how **every sync FastAPI endpoint** does DB work.
- Engines are created lazily **per event loop** (`_engines` dict keyed by `id(loop)`) because asyncpg pools bind to one loop; the process runs two loops (uvicorn main + the `run_async` background loop). Connection: `SUPABASE_DB_URL` env, pool_size=10, max_overflow=20, pre-ping.

### Chats service (`chats/service.py`)
- `build_memory_context(summary: str, turns: List[ChatTurn]) -> str` — formats "Conversation summary:" + "Recent conversation turns:" block for prompt injection.
- `get_chat_session_for_user(db, *, chat_id, user_id) -> ChatSession | None` / `get_or_create_chat_session_for_user(db, *, chat_id, user_id, search_space_id, meta)`.
- `list_recent_turns(db, *, chat_session_id, limit=MEMORY_WINDOW_TURNS)` — last N turns in ascending order.
- `append_turn(db, *, chat_session_id, role, content, ...) -> ChatTurn` — takes `SELECT ... FOR UPDATE` on the session row, then assigns `turn_index = max+1` (serializes concurrent writers).
- `refresh_memory_summary(db, *, chat_session)` — rebuilds `memory_summary` from turns older than the window (truncation-based `"U: ..."/"A: ..."` joiner, **not** an LLM call); env knobs `CHAT_MEMORY_WINDOW_TURNS=8`, `CHAT_MEMORY_SUMMARY_TRIGGER_TURNS=12`, `CHAT_MEMORY_SUMMARY_MAX_CHARS=3000`.
- `serialize_chat_session(db, *, chat_id, user_id) -> dict` — full transcript payload.

### Knowledge (`knowledge/`)
- `KnowledgeManager.list_subjects()` / `list_stores(subject)` / `get_subject(subject)` / `resolve_doc_sets(subject) -> List[Path]` (FAISS dirs, legacy) / `register_store(subject, *, kind, title, index_path, priority)` / `add_pdf_material(subject, pdf_path, *, title, ...)`.
- `TeacherWeeklyStorage` (canonical methods take `search_space_id`): `list_course_by_search_space`, `set_current_week_by_search_space`, `get/update_retrieval_weights_by_search_space`, `enqueue_upload_by_search_space(..., week, kind, pdf_path, title, uploaded_by) -> UploadRecord`, `retry_upload(upload_id)`, `reindex_upload(upload_id)`, `process_next_upload_job()`, `run_upload_worker_loop()`. Name-based wrappers (`list_course(course)`, etc.) resolve slug/name/id first.
- `TeacherPDFIngestor.ingest(pdf_path, *, doc_id, upload_page_asset) -> TeacherPDFIngestionResult` (items, source_markdown, pages, ocr_summary, artifact_manifest, warnings).

### Reports (`reports/ai_use/`)
- `models.create_report(*, chat_id, style, length, markdown, jsonld, model_fingerprint, tool_calls, prompt_hashes)` / `get_report(report_id)` / `list_reports(*, limit=10)` — Supabase PostgREST on `ai_use_reports`.
- `service.build_evidence_pack(chat_id, style, length, *, chat_loader) -> dict` — redacts secrets (sk-/Bearer/api_key regexes), 1000-char excerpts, 16-hex prompt hashes, extracts `[Textbook, p. N]` file refs, dedupes tool calls, drops oldest turns until under `EVIDENCE_TOKEN_BUDGET` (default 8000 tokens).
- `service.generate_report(evidence_pack, style, length) -> dict` — wraps `vendors.openai_client.generate_ai_use_markdown` (markdown + JSON-LD + model fingerprint).
- `pdf.render_pdf_from_markdown(markdown, *, css_paths, metadata) -> bytes`.

### HTTP routes
Mounted routers (in `server.py` ~line 645–686): `reports.ai_use.routes.router` and `chats.routes.router` are `include_router`-ed; knowledge/teacher endpoints are defined inline on `app`.

| Route | Handler location | Notes |
|-------|-----------------|-------|
| `GET/POST/DELETE /chats`, `GET/POST/DELETE /chats/{chat_id}` | `chats/routes.py` | list (with title preview + turn_count), get transcript, upsert (delete-all + reinsert turns), delete; owner-scoped via `resolve_auth_context` |
| `POST /reports/ai-use/{chat_id}`, `GET /reports/ai-use`, `GET /reports/ai-use/{report_id}`, `GET /reports/ai-use/{report_id}.pdf` | `reports/ai_use/routes.py` | every access path re-verifies the caller owns the underlying chat |
| `GET /knowledge/subjects`, `GET/POST /knowledge/stores`, `POST /knowledge/materials` (multipart PDF) | `server.py` ~690–930 | uses `KnowledgeManager`; **no membership check** on these |
| `GET /teacher/weeks`, `POST /teacher/weeks/current`, `GET/POST /teacher/retrieval-weights`, `POST /teacher/upload` (multipart, 202), `POST /teacher/uploads/{upload_id}/retry` (202) | `server.py` ~773–991 | all gated by `_require_course_membership(role="teacher")`; uses `TeacherWeeklyStorage` |
| `POST /ask`, `POST /ask/stream` | `server.py` | consume chats service for memory (see flow 1) |

## Main data flows

1. **Chat turn lifecycle (during `/ask`)** — `server.py:_load_memory_and_append_user_turn` (≈line 407): get-or-create `ChatSession` (400 on `search_space_id` mismatch) → `list_recent_turns` (last 8) → `build_memory_context(memory_summary, recent)` returned to the QA pipeline as prompt context → `append_turn(role="user")` → commit. After the answer, `_append_assistant_turn_and_refresh` (≈line 481): `append_turn(role="assistant", model=SOLVER_MODEL, citations=...)` → `refresh_memory_summary` (turns older than the 8-turn window get squashed into `memory_summary`) → commit. Both bridge via `run_async`.
2. **Chat import/upsert (`POST /chats/{chat_id}`)** — destructive replace: lock session FOR UPDATE, `DELETE` all `chat_turns` for the session, re-`append_turn` each payload turn, then `refresh_memory_summary` and commit.
3. **Teacher upload (async job queue)** — `POST /teacher/upload` → `enqueue_upload_by_search_space`: validate (kind notes|slides|textbook + PDF exists; weekly kinds require week 1..16, `textbook` forces `week=0` and skips the range check — see `_normalize_upload_week`), upload bytes to Supabase Storage bucket `teacher-weekly-uploads`, insert `TeacherUpload(status=queued)` + `TeacherUploadJob(state=queued)`, return 202. Worker (`teacher_upload_worker.py`) polls `_claim_upload_job_async` — `FOR UPDATE SKIP LOCKED` claim with a lease (`TEACHER_UPLOAD_JOB_LEASE_SECONDS`, default 900s) → downloads PDF → `TeacherPDFIngestor.ingest` (native PyMuPDF text; per-page heuristics — low text / image-dominant / equation-like — trigger Mathpix OCR; page PNGs stored to `teacher-weekly-pages` bucket) → `_index_existing_upload_async`: `AITAIndexingService` indexes into `aita_documents`/`aita_chunks`, prior same-week/kind uploads marked `superseded` and their docs set `inactive`, upload set `ready` with `doc_id`, then `_sync_week_activation` flips doc status so only `is_latest` ready uploads with `week <= current_week` are `ready`. Failures: terminal-error pattern match or `attempt_count >= max_retries (3)` → `failed`; else re-queued.
4. **Week change** — `POST /teacher/weeks/current` → `set_current_week_by_search_space` → updates `TeacherCourse.current_week` and re-runs `_sync_week_activation` (bulk `UPDATE aita_documents.status` between `{"state":"inactive"}` and ready), which is how retrieval sees only released material.
5. **AI-use report generation** — `POST /reports/ai-use/{chat_id}`: ownership check → `build_evidence_pack` (loader = `serialize_chat_session` scoped to the user; redact → excerpt → hash prompts → token-budget truncation) → `generate_report` (OpenAI; `TEST_FAKE_OPENAI=1` gives an offline double) → `create_report` persists markdown/JSON-LD to `ai_use_reports` via PostgREST. `GET .../{report_id}.pdf` re-renders markdown with WeasyPrint on each request (PDFs are not stored).
6. **Knowledge CRUD** — `GET /knowledge/subjects|stores` read `aita_search_spaces` + `aita_documents` (store entries are synthesized from `document_metadata.index_path`/`priority`). `POST /knowledge/materials` runs the legacy layout embedder (`knowledge/text-embeder/layout_multimodal_embedder.py`, dynamically imported) producing FAISS+SQLite artifacts on disk, then dual-writes chunks into pgvector via `_index_items_to_pgvector` (failures logged, never raised). `POST /knowledge/stores` registers an existing index dir as an `AITADocument` shell row.

## Key dependencies

- Inbound: `server.py` (`/ask` memory, knowledge/teacher endpoints, router mounts), `apollo/` and `retrieval/` read these tables, `teacher_upload_worker.py` (Procfile worker).
- Outbound: `indexing/` (`AITAIndexingService`, `AITAConnectorDocument`, hashing) for chunk writes; `vendors/supabase_storage.SupabaseStorageClient` (upload buckets); `vendors/supabase_client` (PostgREST for `ai_use_reports`); `vendors/openai_client.generate_ai_use_markdown`; `ocr/mathpix`; `auth.resolve_auth_context`; `config/weights` (`get_env_weights`, `normalize_weights`, `WEIGHT_MIN/MAX`).
- Libraries: SQLAlchemy 2 async + asyncpg, `pgvector.sqlalchemy.Vector`, PyMuPDF (`fitz`), `markdown` + `weasyprint` (PDF), pydantic.

## Non-obvious conventions

- **Two event loops, sync routes**: most endpoints are `def` (not `async def`) and wrap async DB work with `run_async()`. Never share one engine across loops — `database/session.py` keys engines by `id(loop)` to avoid "Future attached to a different loop".
- **Migrations are not Alembic**: numbered files under `database/migrations/`; `001`–`003` are runnable Python one-offs, `004+` are raw SQL applied out-of-band (022 and 023 were applied via Supabase MCP). No revision chain or downgrade. Migration 023 (`023_chunks_halfvec_hnsw.sql`) codifies the `idx_aita_chunks_embedding_hnsw` HNSW expression index (`(embedding::halfvec(3072)) halfvec_cosine_ops`, m=16, ef_construction=64) that was previously hand-created; it is idempotent (`IF NOT EXISTS`) and safe to re-apply.
- **RLS is enabled but unenforced for the backend** (migration 022): backend connects as table owner (exempt unless FORCE); authorization lives in app code (`_require_course_membership`, chat-ownership checks). `ai_use_reports` ownership is purely app-layer.
- **Document status is JSONB**, not an enum column — compare with `DocumentStatus.is_state()`/`status["state"].astext`; teacher_weekly additionally uses an ad-hoc `{"state": "inactive"}` for week gating.
- `reports/ai_use/models.py` deliberately uses Supabase REST, not the SQLAlchemy session — the only module in this domain doing so.
- `turn_index` integrity relies on row-level locks in `append_turn`; don't insert `ChatTurn` rows directly.
- `TeacherCourse.weight_bounds` server_default escapes JSON colons (`\:`) so SQLAlchemy `text()` doesn't treat them as bind params — keep the escaping if touching it.
- Re-index of a ready upload busts the content hash by appending an HTML comment marker (`<!-- reindex:... -->`) to `source_markdown`.
- `knowledge/db.py::DBKnowledgeManager` duplicates the manager interface for a pgvector-only path but is currently unused — check before extending either.
- `EMBEDDING_DIM` is env-driven (`EMBEDDING_DIM`, default 3072, text-embedding-3-large); `ChatSession.topic_centroid_vector` is hardcoded `Vector(3072)`.

## Product context

This domain is Hoot's persistence layer: courses are `SearchSpace`s, course materials live in `aita_documents`/`aita_chunks` for RAG retrieval, teachers release content week-by-week (week gating directly controls what students can retrieve), student conversations persist as chat sessions with rolling memory so follow-ups have context, and AI-use reports give students/teachers an auditable, citation-aware PDF record of how AI was used in a chat — supporting academic-integrity workflows.
