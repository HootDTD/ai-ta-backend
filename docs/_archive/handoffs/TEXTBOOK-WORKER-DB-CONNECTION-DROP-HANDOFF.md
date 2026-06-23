# Handoff — Textbook indexing job fails at final DB write (connection dropped mid-operation)

**Date:** 2026-06-12
**Author:** prior Claude Code session (read-only investigation — no code/DB changes made)
**Status:** Root cause confirmed. No fix applied. Upload `id=2` is now `status=failed` on the TEST project.
**Environment:** Railway **staging** worker → **TEST** Supabase (`hjevtxdtrkxjcaaexdxt`).

---

## TL;DR

A course-wide textbook upload (`fluidMechanics.pdf` = *"Basics of Fluid Mechanics"*, Genick
Bar‑Meir) was being processed by the staging upload worker. The worker did **all** the real
work — downloaded the PDF, OCR'd it, and embedded the **entire** textbook via OpenAI
`text-embedding-3-large` (≈1h42m of work) — and then **died on the very last step**: the
single `UPDATE aita_documents` that flips the doc to `ready`.

**Root cause:** the indexing path holds **one Postgres connection/session open across the
whole ~1.5‑hour job**. Over that window the Supabase connection is reaped, so the terminal
write hits a dead socket:

```
asyncpg.exceptions.ConnectionDoesNotExistError: connection was closed in the middle of operation
[SQL: UPDATE aita_documents SET content=$1, embedding=$2, status='{"state":"ready"}',
      updated_at=$4 WHERE aita_documents.id = 2]
```

That poisoned the session (`PendingRollbackError`), the attempt failed, and after 3 attempts
the worker logged `Teacher upload job exhausted retries upload_id=2 attempts=3` and marked the
upload `failed`. **Every attempt redoes the full ~1.5h OCR+embedding (real OpenAI $) and then
dies at the same write** — so a textbook this size can never reach `ready` as the code stands.

This is **separate** from two other issues found the same day (see "Not this bug" below).

---

## Evidence (how we know)

### 1. Final state of the job (TEST Supabase `hjevtxdtrkxjcaaexdxt`)
`teacher_uploads` / `teacher_upload_jobs`, `upload_id = 2`:

| field | value |
|---|---|
| `status` / `job_state` | **`failed`** |
| `doc_id` | `null` |
| `page_count` | `null` |
| `artifact_manifest->pages` | `[]` (0) |
| `attempt_count` | `3` (exhausted) |
| `started_at` | `2026-06-12 16:02:05Z` |
| `completed_at` | `2026-06-12 17:44:08Z` (≈1h42m later) |
| `error_message` / `last_error` | the `PendingRollbackError` / `ConnectionDoesNotExistError` text below |
| `created_at` | `2026-06-11 23:22:42Z` |

Storage object (exists, not the problem):
`search-space-2/week-00/textbook/d9d4fa4bd4994abcabbbe5476deca731/fluidMechanics.pdf`
(`search_space_id=2`, `week=0`, `kind=textbook`).

### 2. The worker traceback (Railway, staging `ai-ta-backend-worker`)
Deployment `a828f486` (commit `07f92a0`, PR #15), 2026-06-12T17:44:08Z:

```
sqlalchemy.exc.DBAPIError: (asyncpg ... Error)
  <class 'asyncpg.exceptions.ConnectionDoesNotExistError'>: connection was closed in the middle of operation
[SQL: UPDATE aita_documents SET content=$1::VARCHAR, embedding=$2, status=$3::JSONB,
      updated_at=$4::TIMESTAMP WITH TIME ZONE WHERE aita_documents.id = $5::INTEGER]
[parameters: ('Basics of\nFluid\nMechanics By\nGenick Bar–Meir ...'(truncated),
             '[-0.0382995... 3072-dim halfvec ...]'(60k chars), '{"state": "ready"}',
             datetime(2026,6,12,17,44,7), 2)]

During handling of the above exception, another exception occurred:
sqlalchemy.exc.PendingRollbackError: This Session's transaction has been rolled back due to a
  previous exception during flush. ... Original exception was: ConnectionDoesNotExistError ...

2026-06-12 17:44:08,677 WARNING Teacher upload job exhausted retries upload_id=2 attempts=3
```

> **Log-reading note:** Railway tags **all** worker stdout/stderr lines as `severity:"error"`
> (the worker writes to stderr). Most "error" lines in this service are actually `INFO`
> (e.g. the `... POST /v1/embeddings "HTTP/1.1 200 OK"` stream). **This** block is a genuine
> Python traceback — don't dismiss it as the coloring artifact.

### 3. The work actually happened
From ~16:11–17:44 the worker emitted a dense, continuous stream of
`INFO HTTP Request: POST https://api.openai.com/v1/embeddings "HTTP/1.1 200 OK"` — i.e. it
OCR'd then embedded the whole textbook. The failure is **only** at the terminal persistence
write, after all compute was spent.

---

## Root cause analysis

A single `AsyncSession`/connection is held open across the entire indexing run (OCR → thousands
of embedding calls → final `UPDATE`). Postgres/Supabase reaps the connection during that long
window; the last statement then fails with `ConnectionDoesNotExistError`.

Engine config (`ai-ta-backend/database/session.py:85`):
```python
create_async_engine(
    database_url,          # SUPABASE_DB_URL
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,    # validates at CHECKOUT only — cannot save a conn held 1.5h then used
)
# NOTE: no pool_recycle
```

- `pool_pre_ping` pings on checkout, **not** during use, so it can't catch a connection that
  dies while held mid-job.
- No `pool_recycle`, so nothing proactively refreshes a long-lived connection.
- The terminal `UPDATE` ships a large payload (content + 3072-dim halfvec). Not the cause, but
  relevant if a pooler statement-size/prepared-statement limit is also in play (verify pooler —
  see step 4).

### Secondary defect surfaced by the same run
The job's lease is set **once at claim** for `TEACHER_UPLOAD_JOB_LEASE_SECONDS` (default 15 min,
`teacher_weekly.py:246`, lease written at `:914`) with **no heartbeat/renewal**. This job ran
102 min, so the lease expired at 16:17 while it was still working. It survived only because
staging runs a **single** worker replica (one `lease_owner`, pid 1) that won't poll for a new job
until the current one returns. With ≥2 replicas, a second worker would re-claim the
"expired" job and run a duplicate. Fix this alongside the connection issue if scaling the worker.

---

## Code references (backend repo @ commit `07f92a0`)

- `knowledge/teacher_weekly.py:586` `_process_claimed_upload_job` — try/except wrapper; only logs on exception
- `knowledge/teacher_weekly.py:599` `run_async(self._index_existing_upload_async(...))` — the failing call
- `knowledge/teacher_weekly.py:1034` `indexed_doc = await service.index_from_items(docs[0], connector_doc, items)`
- `indexing/indexing_service.py:207` `log.error("Indexing failed for document '%s': %s", ...)` — where the inner failure is logged
- `database/session.py:78` `run_async(...)` (sync→async bridge); `database/session.py:85` `_build_engine()` (engine config above)
- Lease: `knowledge/teacher_weekly.py:246` `job_lease_seconds`; `:914` lease set at claim (no renewal)
- Worker loop / boot log: `knowledge/teacher_weekly.py:348` `run_upload_worker_loop`, `:350` "Teacher upload worker started"
- Manual retry path (resets a `failed` upload): `knowledge/teacher_weekly.py:756` `_retry_upload_async` (decrements `attempt_count` by 1, sets `status=queued`)

---

## Recommended fix (ordered; nothing applied yet)

1. **Don't hold a DB connection across the embedding loop.** Restructure
   `_index_existing_upload_async` / `index_from_items` so OCR + embedding run with **no open DB
   transaction**, then open a **fresh, short-lived `AsyncSession`** only for the final
   persistence write (so that connection is seconds old, not 1.5h). This is the real cure.
2. **Commit incrementally** (per page/batch) so partial progress survives and a retry resumes
   mid-way instead of re-embedding the whole book (saves OpenAI $ and wall-clock on retries).
3. **Add `pool_recycle`** (e.g. `1800`, below Supabase's idle timeout) and keep `pool_pre_ping`.
   Backstop only — does **not** replace #1.
4. **Verify the pooler.** Read `SUPABASE_DB_URL` on the staging worker
   (`ai-ta-backend-worker`). If it points at the Supabase **transaction pooler** (port `6543`,
   pgbouncer), asyncpg also needs `statement_cache_size=0` and likely `NullPool`; a long idle
   then large prepared statement can drop the connection. (The 1.5h hold is sufficient to
   explain the drop on its own, but rule this out.)
5. **Make the terminal write resilient:** wrap it in its own `try` with a fresh session + a
   bounded retry, and ensure `error_message` is always written on failure (it was, here).
6. **Lease heartbeat / size:** add lease renewal during long processing, or raise
   `TEACHER_UPLOAD_JOB_LEASE_SECONDS`, before running >1 worker replica (see secondary defect).

---

## Verification / repro for the next session

- **Re-trigger:** `upload_id=2` is `failed`, so the manual retry path applies
  (`_retry_upload_async` requires `status=failed`). Re-queue it and watch the staging worker
  logs: expect the `/v1/embeddings 200 OK` stream, then the final `UPDATE aita_documents`.
  Pre-fix it will fail again at ~the same place; post-fix it should reach `status=ready` with a
  non-null `doc_id` and an `aita_documents` row (with `week IS NULL` for a textbook).
- **Regression test (per coverage contract):** add a Testcontainers Postgres test that drops/kills
  the connection between the embedding step and the final write, asserting the worker either
  resumes or fails cleanly **without** re-doing all embedding. Patch coverage ≥95% on changed
  lines; DB work is **local Docker only** — never apply migrations to a remote project.
- **Drift contract:** after the code change, update the owner docs in the same commit —
  `ai-ta-backend/docs/architecture/indexing.md` (owns `indexing/`, `ocr/`) and
  `.../domain-data.md` (owns `knowledge/`, `database/`) — and bump `last_verified`.

---

## Reference IDs / paths

**Railway** (project `hoot-ai-ta` = `8f327b70-9f91-43b1-a4fe-2ad1323a326c`)
| Thing | ID |
|---|---|
| staging env | `ccf25b85-9da0-4e4e-9ac9-c60ab7b586ce` |
| svc `ai-ta-backend-worker` (staging) | `6301a977-11f9-45d3-af24-1afd0fd19984` |
| worker deployment (failed run) | `a828f486-51af-42d7-a7b4-b3bf2577bf96` (commit `07f92a0`, PR #15) |
| worker instance (lease_owner) | `091ebc874c91:1:b845aeb9` |
| svc `ai-ta-backend` (staging) | `f488eb48-ef24-4fec-b76e-4db70faab232` |

**Supabase**
- Staging worker writes to **TEST** project `hjevtxdtrkxjcaaexdxt` (confirmed in prior
  TEXTBOOK-STAGING-HANDOFF.md via `pg_stat_activity`).
- Tables: `teacher_uploads`, `teacher_upload_jobs` (queue), `aita_documents` (the failing write target).

**Failed record:** `teacher_uploads.id = 2`, `search_space_id=2`, `week=0`, `kind=textbook`,
`title=fluidMechanics`, `source_name=fluidMechanics.pdf`.

---

## Not this bug (separate issues found the same session — don't conflate)

1. **Apollo `POST /apollo/sessions/from_hoot` → 422 on staging.** Frontend/backend deploy skew:
   the deployed staging student-UI (`8bd3c905`, PR #5) still sends the **old** payload
   `{ student_id, hoot_transcript }`, but the auth-scoped backend now requires `search_space_id`.
   Fix = merge student-UI branch `feat/apollo-auth-scoping` (`ce99e68`) → `staging` and redeploy.
2. **NUL-byte sanitization** (PR #15) — already merged; it addressed a *different* failure mode
   (NUL bytes at the indexing boundary), not this connection-lifetime defect.
3. **Migration `024`** (relaxes `teacher_uploads` week/kind checks) — applied on TEST only; still
   required for prod. See TEXTBOOK-STAGING-HANDOFF.md.
