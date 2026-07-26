---
doc: knowledge/teacher-weekly
description: TeacherWeeklyStorage — teacher uploads over Supabase Storage + a durable leased job queue + the worker loop.
owns:
  - knowledge/teacher_weekly.py
  - knowledge/__init__.py
related:
  - knowledge/teacher-pdf-ingestion
  - indexing/checkpoint-indexer
  - indexing/indexing-service
  - rag-pipeline/document-visibility
  - platform/config-weights
  - platform/vendor-supabase-storage
  - database/models
last_verified: 2026-07-25
stub: false
---

# knowledge/teacher-weekly — uploads, job queue, worker loop

Known monolith (~1301 lines). Supabase-first, backed by the shared ORM
(`app.courses`, `app.documents`, `internal.document_chunks`, `app.uploads`).
`knowledge/__init__.py` is an empty package marker. Canonical methods take
`search_space_id`; name-based wrappers resolve slug/name/id first.

## Interface

- `list_course_by_search_space` (per-week notes/slides grid + course-level
  textbook section via `_assemble_course_payload`).
- `set_current_week_by_search_space` (re-runs `_sync_week_activation`).
- `get_/update_retrieval_weights_by_search_space`.
- `enqueue_upload_by_search_space(...) -> UploadRecord`; `retry_upload`,
  `reindex_upload`.
- `process_next_upload_job`, `run_upload_worker_loop` (the Procfile worker).
- Dataclasses `UploadRecord` / `ClaimedUploadJob`; constants
  `WEEKLY_KINDS={notes,slides}`, `COURSE_WIDE_KINDS={textbook}`,
  `COURSE_WIDE_WEEK=0`, plus upload-status / job-state enums.

## Data flow

**Enqueue**: validate (kind + PDF; weekly kinds weeks 1..N, textbook forced to
week 0 via `_normalize_upload_week`) → upload to bucket `teacher-weekly-uploads` →
insert `Upload(queued)` + `UploadJob(queued)` → return 202.
**Worker**: `_claim_upload_job_async` claims via `FOR UPDATE SKIP LOCKED` with a
lease → download → `TeacherPDFIngestor.ingest` (`teacher-pdf-ingestion`) →
`_index_existing_upload_async` (three short-session phases: doc upsert + resume
pointer; checkpointed embed+persist via `indexing/checkpoint-indexer` with lease
renewal + `attempt_count` reset per batch; finalize) → `_sync_week_activation`.
**Finalize** marks the upload READY, supersedes prior same-week/kind latest docs
(`is_latest=False`, status superseded), and flips document status so only latest
ready uploads with `week <= current_week` are visible.

## Invariants & gotchas

- Storage ensure-first seams: memoized `_ensure_buckets` auto-creates the
  upload/pages buckets; page PNGs upsert.
- Checkpointing holds **no DB session across an OpenAI embed call** (asyncpg
  conn-drop protection); progress resumes from
  `artifact_manifest.embed_progress.last_completed_page`.
- **No apollo autoprovision enqueue in this module** on staging — the finalize
  block does document supersession + week activation only. (Historical designs
  described a SAVEPOINT-guarded `apollo.provisioning.enqueue` hook here; it is not
  present in this tree — do not reintroduce it as fact.)
- Retrieval weights are clamped/normalized via `platform/config-weights`.

## Env flags

`TEACHER_UPLOAD_JOB_LEASE_SECONDS`, `DEFAULT_MAX_RETRIES` (`TEACHER_UPLOAD_*`),
`TOTAL_WEEKS_DEFAULT` overrides, bucket-name + poll-seconds envs,
`TEACHER_UPLOAD_RENDER_DPI` (via the ingestor).

## Related

`knowledge/teacher-pdf-ingestion`, `indexing/checkpoint-indexer`,
`indexing/indexing-service`, `rag-pipeline/document-visibility`,
`platform/config-weights`, `platform/vendor-supabase-storage`, `database/models`.
