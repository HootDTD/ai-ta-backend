# Handoff — Textbook upload on staging: 3 of 4 stacked failures fixed; NUL-byte indexing bug remains

**Date:** 2026-06-11 (late session)
**Author:** Claude Code session (continuation of `docs/TEXTBOOK-STAGING-HANDOFF.md`)
**Status:** Upload `id=2` ("fluidMechanics.pdf", Fluids E2E, search_space_id=2) is `failed` on staging.
The PDF + all 873 page images are in storage; **zero** `aita_documents`/`aita_chunks` rows exist,
so retrieval correctly returns nothing for the course. One code bug blocks completion.

> **UPDATE 2026-06-12:** The fix below (all 3 parts) is BUILT —
> **PR #15** `fix/textbook-nul-bytes` → `staging`
> (https://github.com/HootDTD/ai-ta-backend/pull/15). NUL sanitization at the
> DTO + chunker chokepoints (`indexing/text_sanitization.py`), bucket
> auto-ensure (`SupabaseStorageClient.ensure_bucket` + memoized
> `_ensure_buckets()`), `upsert=true` page uploads. Real-Postgres regression
> test reproduces the exact failure; 658 passed, 100% patch coverage.
> Remaining: merge PR #15, Railway auto-deploys staging, then Retry upload
> id=2. Failure #4 (migration `024`) is still a pending human step for prod.

---

## TL;DR — the four stacked failures

A teacher textbook upload on staging tripped four independent problems, each only visible
after the previous one was fixed. First-ever end-to-end run of this path against a real
environment; the existing tests mock storage and the DB.

| # | Symptom | Root cause | Status |
|---|---------|-----------|--------|
| 1 | `400` at `POST /teacher/upload` (22:08) | Deploy skew: teacher-UI textbook code deployed, backend textbook branch never merged | **FIXED** — PR #13 merged to `staging` 23:03, deployed (`f837d1ab`) |
| 2 | `500` at upload (23:09) | Staging's Supabase project (**test**, `hjevtxdtrkxjcaaexdxt`) had **zero storage buckets**; prod's were created manually, never mirrored | **FIXED** — `teacher-weekly-uploads` + `teacher-weekly-pages` created (private) via SQL on test, 2026-06-11 |
| 3 | `202` accepted, worker fails after full OCR (23:22→23:45, 3 attempts) | **Extracted text contains NUL bytes (`\x00`)** → Postgres `CharacterNotInRepertoireError` → document never persisted → `RuntimeError("Failed to resolve indexed document for teacher upload")` | **OPEN — the remaining blocker.** Fix proposed below, not yet approved/built |
| 4 | (not yet hit) first textbook INSERT on prod will violate `teacher_uploads` CHECKs | Migration `024_teacher_textbook.sql` exists in repo but is **applied nowhere** | **PENDING HUMAN STEP** — apply to test, then prod BEFORE prod code deploy |

Key architectural fact for whoever picks this up: **students never retrieve from buckets.**
Buckets hold the raw PDF + page PNGs only. Retrieval searches `aita_documents`/`aita_chunks`
(+ pgvector embeddings) in Postgres. Pipeline: PDF → bucket → OCR/extract → Postgres rows →
retrieval. It died at the Postgres write.

---

## Evidence for the open bug (#3)

Worker logs, Railway svc `ai-ta-backend-worker` (staging), deploy `a7f8a2bf`, attempts at
23:31:56 / 23:39:01 / 23:45:06:

```
ERROR Batch prepare failed: (sqlalchemy.dialects.postgresql.asyncpg.Error)
  <class 'asyncpg.exceptions.CharacterNotInRepertoireError'>:
  invalid byte sequence for encoding "UTF8": 0x00
...
RuntimeError: Failed to resolve indexed document for teacher upload
```

The logged INSERT parameters show the full extracted text (~1,577,768 chars, 873 pages,
PyMuPDF + Mathpix-fallback extraction of a scanned textbook) headed for `aita_documents`.
Postgres `TEXT` rejects `\x00`. The "Failed to resolve indexed document" error is the
*downstream symptom*: `prepare_for_indexing` fails, no row exists, the fallback
hash-lookup at `knowledge/teacher_weekly.py:1012-1022` finds nothing, and
`knowledge/teacher_weekly.py:1025` raises.

DB state (test project, verified 2026-06-11 ~23:50 UTC):

- `teacher_uploads` id=2: `status='failed'`, `attempt_count=3`,
  `error_message='Failed to resolve indexed document for teacher upload'`,
  `storage_key='search-space-2/week-00/textbook/d9d4fa4bd4994abcabbbe5476deca731/fluidMechanics.pdf'`
- `storage.objects`: 1 object in `teacher-weekly-uploads`, **873** in `teacher-weekly-pages`
- `aita_documents` / `aita_chunks` for search_space_id=2: **0 rows each**

Red herring: the upload's `artifact_manifest` warnings contain hundreds of
`400 Client Error ... /teacher-weekly-pages/.../page-NNNN.png` entries. Those are retries
#2/#3 re-uploading already-existing page PNGs with `x-upsert: false` → duplicate rejection.
Warnings only, NOT the failure cause.

Cost note: each worker retry re-downloads and re-OCRs the entire 873-page book (~8 min per
attempt). Anything that fails post-OCR burns three full OCR runs per upload attempt.

---

## Proposed fix (designed, NOT yet built — awaiting user approval)

1. **NUL sanitization at the ingestion boundary** (the blocker). Strip `\x00` from extracted
   document content, markdown, title, and chunk/item text before anything reaches SQLAlchemy —
   in `knowledge/teacher_pdf_ingestion.py` output and/or `indexing/` prepare path (place it
   where ALL ingest routes pass through; check `AITAIndexingService.prepare_for_indexing`).
   TDD: RED test pushing NUL-containing content through the prepare path against real
   Postgres (Testcontainers harness, `tests/conftest.py::db_session` or the migration-applying
   pattern in `tests/database/test_teacher_uploads_constraints.py`).
2. **`upsert=true` for worker page-artifact uploads** (`knowledge/teacher_weekly.py:461`
   area) so job retries don't generate hundreds of duplicate-object warnings.
3. **Auto-ensure buckets** (user-proposed, agreed): add `ensure_bucket()` to
   `vendors/supabase_storage.py` (POST `/storage/v1/bucket`, tolerate already-exists) +
   memoized `_ensure_buckets()` in `TeacherWeeklyStorage` before first storage use, covering
   both `upload_bucket` and `pages_bucket`. Kills failure #2 permanently for new
   environments. Bucket names are app-owned constants/env — not user input — so the
   "auto-create masks typos" concern doesn't apply.

Branch off `staging`, repo gates apply: full suite, 95% patch coverage
(`diff-cover --compare-branch=origin/staging`), ruff `check` **and** `format --check` on
ADDED files (CI blocks on format — bit us once already this session), drift-contract doc
updates (`domain-data.md` owns `knowledge/` + `database/`; `_overview.md` likely owns
`vendors/` — check frontmatter `owns:`).

**After merge+deploy:** click Retry on upload id=2 (or `POST /teacher/uploads/2/retry`) —
PDF is already in the bucket; it re-processes in place. Expect: `status` → `ready`,
an `aita_documents` row (`material_kind='textbook'`, `week IS NULL`), chunks + embeddings,
and the Fluids E2E student chat retrieving from the textbook with citations.

---

## Deploy/infra context (changed today)

- **PR #13 merged to `staging`** (merge commit `339611c`): textbook backend + migration
  `024` file + real-DB constraint tests (`tests/database/test_teacher_uploads_constraints.py`)
  + doc reconciliation. CI green (`ci-passed`; mypy job red is advisory-by-design).
- **Staging backend/worker → TEST Supabase project** (`hjevtxdtrkxjcaaexdxt`), proven via
  `pg_stat_activity` timestamp correlation (Railway hides env values from its agent; raw env
  dumps are classifier-blocked — use the correlation trick).
- Test project `teacher_uploads` has **no week/kind CHECKs** (drift vs migration 004 —
  schema likely from `Base.metadata.create_all`, which declares no CHECKs). So staging never
  hits failure #4; **prod will** until `024` is applied.
- **Migration `024_teacher_textbook.sql`**: in repo, applied to NO remote project. Human/CI
  step: apply to test first, then prod, BEFORE prod gets the textbook code (same
  migration-before-code ordering as PR #12/migration 023).
- **`023` numbering collision** in `database/migrations/` (`023_apollo_auth_scoping.sql` +
  `023_chunks_halfvec_hnsw.sql`). Next migration takes `025`. Documented in
  `docs/shared-architecture/supabase.md`.
- Buckets on test were created via
  `INSERT INTO storage.buckets (id, name, public) VALUES (..., false)` — settings are
  defaults (no size limit / MIME restriction). Prod's manually-created buckets may have
  different settings; compare before relying on parity.

## Reference IDs

| Thing | Value |
|---|---|
| Railway project `hoot-ai-ta` | `8f327b70-9f91-43b1-a4fe-2ad1323a326c` |
| staging env | `ccf25b85-9da0-4e4e-9ac9-c60ab7b586ce` |
| svc `ai-ta-backend` | `f488eb48-ef24-4fec-b76e-4db70faab232` (deploy `f837d1ab`, commit `339611c`) |
| svc `ai-ta-backend-worker` | `6301a977-11f9-45d3-af24-1afd0fd19984` (deploy `a7f8a2bf`) |
| svc `ai-ta-teacher-ui` | `784eff94-9ab6-47bf-91d2-b9205e358c81` |
| Supabase test (staging's DB) | `hjevtxdtrkxjcaaexdxt` |
| Supabase prod (constraints UNVERIFIED, queries need explicit user auth) | `uduxdniieeqbljtwocxy` |
| Failed upload | `teacher_uploads.id=2`, job_id=2, search_space_id=2 (Fluids E2E) |
| Backend PR #13 | https://github.com/HootDTD/ai-ta-backend/pull/13 (merged) |

## Code refs (post-merge `staging`)

- `knowledge/teacher_weekly.py:1003-1028` — index + resolve + the raised error
- `knowledge/teacher_weekly.py:659` — enqueue PDF→bucket; `:461` — worker page upload (no upsert)
- `knowledge/teacher_pdf_ingestion.py` — PyMuPDF extraction + Mathpix fallback (NUL source)
- `vendors/supabase_storage.py` — bare REST client; no ensure/create bucket anywhere in repo
- `database/migrations/024_teacher_textbook.sql` — week `0..16`, kind incl. `'textbook'`
- `tests/database/test_teacher_uploads_constraints.py` — migration-applying Postgres harness pattern

## Tooling gotchas (this session)

- Railway MCP `get-logs` `filter` is Loki-style; a plain substring (e.g. `error`) works well.
- Railway agent **cannot read service variable values** (hidden); env dumps also
  classifier-blocked. Identify the DB by `pg_stat_activity` correlation instead.
- PS 5.1: `2>$null` on a native command sets `$?` false even on exit 0 — broke a chained
  `pytest && diff-cover`; run diff-cover separately.
- `gh` CLI not installed; use GitHub MCP. CI job logs aren't readable unauthenticated —
  reproduce CI steps locally from `.github/workflows/ci.yml` instead.
- Worker retries re-OCR everything; keep that in mind before "just retry it" debugging.
