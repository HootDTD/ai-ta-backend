# Handoff — Textbook upload fails on staging (and the latent bug behind it)

**Date:** 2026-06-11
**Author:** prior Claude Code session (debugging, read-only — no code/DB changes made)
**Status:** Root cause confirmed. No fix applied yet. Two follow-up actions defined below.

---

## TL;DR

A teacher tried to upload a **course textbook** on staging and it "failed."
The user's mental model was "crash in embedding / DB storage." **That is not what
happened.** The request was rejected at HTTP validation with **`400 Bad Request`**
and never reached embedding or the database.

**Root cause = frontend-ahead-of-backend deploy skew:**

- **teacher UI** textbook code **is** deployed to staging → it POSTs `kind=textbook, week=0`.
- **backend** textbook code is **NOT** deployed (and never merged anywhere) → staging
  backend still only accepts `kind ∈ {notes, slides}` and rejects `textbook` with 400.

There is also a **latent, separate defect** in the textbook commit that will turn the
400 into a **500** the moment the backend *is* deployed (missing DB migration). Details below.

---

## Evidence (how we know)

### 1. The actual staging log line
Railway, service `ai-ta-backend` (staging), deployment `ac5ad71e`:

```
2026-06-11T22:08:19Z  POST /teacher/upload  →  400 Bad Request  (2128 ms)
host: ai-ta-backend-staging.up.railway.app   srcIp: 152.55.177.67 (the teacher-ui proxy)
```

- Preceding requests (`GET /teacher/weeks`, `/teacher/retrieval-weights`) all `200`.
- The 2.1 s duration = the PDF body uploaded, *then* validation rejected it.

### 2. The worker did nothing
Service `ai-ta-backend-worker` (staging), deployment `79028940`:
```
2026-06-11 16:50:52  INFO Teacher upload worker started ...
```
…and nothing else. **No textbook processing, no embedding, no DB insert.** Confirms the
upload was rejected before the queue/worker stage.

### 3. What the staging backend actually runs
Deployment `ac5ad71e` builds from **branch `staging`, commit `6a6fdac`**
("Merge pull request #12 from HootDTD/feat/apollo-auth-scoping"). At that commit:

```python
# knowledge/teacher_weekly.py  (deployed version, 6a6fdac)
VALID_KINDS = {"notes", "slides"}
...
if kind_norm not in VALID_KINDS:
    raise ValueError("kind must be 'notes' or 'slides'")   # -> HTTPException(400) at server.py:983
```

So `kind=textbook` → `ValueError` → 400. Exactly matches the log.

### 4. The textbook backend code is unmerged
- Textbook backend lives **only** on branch `teacher-textbook-upload`,
  commit **`28e687c`** ("textbook upload update"), pushed to `origin` but with **no PR**
  (open or closed) against any base.
- `git merge-base --is-ancestor 28e687c <branch>` → **NOT** contained in
  `origin/staging`, `origin/ApolloV3`, `origin/ApolloV4`, or `origin/main`.
- **PR #12 is the Apollo-auth retrofit, not textbook.** Verified its full 30-file list:
  all `apollo/*`, `database/migrations/023_apollo_auth_scoping.sql`, CI, docs — **zero**
  mentions of `textbook` / `teacher_weekly` / `VALID_KINDS`. (This was the user's mix-up.)
- The **teacher-UI** textbook commit `a6f9944` (branch `textbook-upload`) **is** deployed
  to staging (`ai-ta-teacher-ui` deploy `58fc3739`, 2026-06-11T21:15).

---

## The latent defect in commit `28e687c` (will 500 once deployed)

The textbook commit changes the write path but **ships no migration**:

```
git show --stat 28e687c
 docs/architecture/domain-data.md               |   8 +-
 knowledge/teacher_weekly.py                    | 129 +++++++---
 server.py                                      |   6 +
 tests/integration/test_teacher_textbook_api.py | 147 +++++++++++
 tests/unit/test_teacher_textbook.py            | 341 +++++++++++++++++++++++++
   --> NO database/migrations/*.sql
```

New code writes `week=0` (sentinel `COURSE_WIDE_WEEK`) and `kind='textbook'` into
`teacher_uploads` (`knowledge/teacher_weekly.py:668-670`). But that table
(`database/migrations/004_teacher_features.sql:24`) still declares:

```sql
week  INTEGER NOT NULL CHECK (week BETWEEN 1 AND 16),
kind  TEXT    NOT NULL CHECK (kind IN ('notes','slides')),
```

No later migration relaxes either. **On any DB where those checks are live, the INSERT
raises `check_violation` → 500 at upload.** (The indexed-document `week=NULL` is fine —
`aita_documents.week` is nullable: `database/models.py:140`.)

### Why the commit's green tests do NOT catch this
Both new tests bypass the real Postgres constraints:
- `tests/unit/test_teacher_textbook.py` — pure functions, no DB.
- `tests/integration/test_teacher_textbook_api.py` — uses
  `SUPABASE_DB_URL=sqlite+aiosqlite:///:memory:` **and** monkeypatches
  `_get_teacher_storage` to a fake `_Storage`. `test_upload_accepts_textbook_kind`
  asserts `202` against a **mock** `enqueue_upload_by_search_space` — the real
  Postgres INSERT never runs.

So the suite is green while the one thing that breaks in prod is exactly what's mocked.
This conflicts with the workspace test-coverage contract's intent (the changed write
path has no test exercising the real constraint).

---

## OPEN QUESTION — RESOLVED 2026-06-11 (next session)

> **Answer: the staging backend writes to the TEST project (`hjevtxdtrkxjcaaexdxt`).**
> Railway hides variable values from both the MCP agent and the auto-mode classifier
> blocked a raw env dump, so it was settled behaviorally via `pg_stat_activity` on the
> test project: a pooled `postgres` connection with `backend_start = 16:50:53` matches
> the staging worker's "Teacher upload worker started" log (16:50:52), and connections
> opened 22:07:55–22:07:57 show activity at exactly 22:08:19.445 — the failed upload's
> timestamp. Test project lacks the week/kind checks, so on staging the INSERT would
> have succeeded — but migration `024` is still required for prod and to close the
> schema drift. Follow-ups 2 (migration 024) and 3 (real-DB constraint test) are done
> on branch `teacher-textbook-upload`; see PR. Original open question kept below.

## OPEN QUESTION — original text (superseded)

**Which Supabase project does the staging backend write to, and does that DB carry the
`004` check constraints?** This decides whether the latent defect actually bites.

What we know:
- **Test** project `hjevtxdtrkxjcaaexdxt`: `teacher_uploads` has **only**
  `teacher_uploads_status_check` — the `week`/`kind` checks are **absent**. (Verified via
  `supabase-test` MCP.) So if staging writes here, the textbook INSERT would *succeed*.
- **Prod** project `uduxdniieeqbljtwocxy`: **NOT verified.** The read-only
  `pg_constraint` query was blocked by the Claude Code auto-mode classifier because the
  task only authorized "staging logs," not a prod query. Needs explicit user authorization.
- Workspace docs say Railway → prod Supabase, but the local backend `.env` points at
  **test**. Staging service env was not read. **Next session must read the staging
  `ai-ta-backend` service variable `SUPABASE_DB_URL` from Railway to settle this.**

---

## Recommended next steps

1. **Confirm the target DB + its constraints**
   - Read `SUPABASE_DB_URL` (and `SUPABASE_URL`) on Railway staging service
     `ai-ta-backend` (`f488eb48-...`).
   - Against that exact project, run (read-only):
     ```sql
     SELECT conname, pg_get_constraintdef(oid)
     FROM pg_constraint
     WHERE conrelid = 'public.teacher_uploads'::regclass AND contype = 'c';
     ```
     If the `week`/`kind` checks are present → the 500 is real; do step 2 first.
     If absent → INSERT will pass, but **add them or a deliberate decision**, because
     prod-vs-test schema drift is itself a hazard.

2. **Write migration `024_teacher_textbook.sql`** (the real fix for the latent bug)
   - Relax `teacher_uploads_week_check` → allow `0` (e.g. `week BETWEEN 0 AND 16`
     or `week >= 0`).
   - Relax the kind check → `kind IN ('notes','slides','textbook')`.
   - Idempotent (drop-constraint-if-exists + add), mirrored to **test** project first,
     then prod is a human/CI step (agents never apply migrations to remote DBs — see
     workspace rules).
   - Note: the unique index `uniq_teacher_uploads_latest (search_space_id, week, kind)
     WHERE is_latest` stays correct with `week=0` fixed (one latest textbook per course).

3. **Add a real-DB integration test** (Testcontainers Postgres, per existing harness)
   that inserts a `week=0, kind='textbook'` row and asserts it persists — so the
   constraint can never silently regress. Current tests mock this away.

4. **Open the PR** `teacher-textbook-upload` → `staging`, get CI green (incl. the 95%
   patch-coverage gate), and **deploy in the right order**: apply migration `024` to the
   DB the backend writes to **before** merging/deploying the backend code — same
   migration-before-code dependency PR #12 had with `023`. Otherwise the first textbook
   upload 500s.

5. After backend ships, end-to-end test one real textbook upload on staging (UI is
   already deployed) and confirm: `202` at `/teacher/upload`, worker logs OCR/embedding,
   a `teacher_uploads` row with `week=0, kind='textbook', status` progressing to `ready`,
   and an `aita_documents` row with `week IS NULL`.

---

## Reference IDs / paths (so the next session doesn't re-discover them)

**Railway** (project `hoot-ai-ta` = `8f327b70-9f91-43b1-a4fe-2ad1323a326c`)
| Thing | ID |
|---|---|
| staging env | `ccf25b85-9da0-4e4e-9ac9-c60ab7b586ce` |
| production env | `80e7cfde-37ed-4610-bdb8-c233a111e0ea` |
| svc `ai-ta-backend` | `f488eb48-ef24-4fec-b76e-4db70faab232` (staging deploy `ac5ad71e`, commit `6a6fdac`, 16:48) |
| svc `ai-ta-backend-worker` | `6301a977-11f9-45d3-af24-1afd0fd19984` (deploy `79028940`) |
| svc `ai-ta-teacher-ui` | `784eff94-9ab6-47bf-91d2-b9205e358c81` (staging deploy `58fc3739`, 21:15) |
| svc `ai-ta-student-ui` | `659d4971-e146-41a7-b4f9-2731cec812f3` |

**Git**
- Backend textbook: commit `28e687c` on `teacher-textbook-upload` (+ `origin/`). No PR. Not in staging/ApolloV3/ApolloV4/main.
- Teacher-UI textbook: commit `a6f9944` on `textbook-upload` (deployed to staging).
- Staging backend HEAD: `6a6fdac` (PR #12, apollo auth).

**Code refs (backend repo, at `28e687c`)**
- `knowledge/teacher_weekly.py:55` `COURSE_WIDE_WEEK = 0`
- `knowledge/teacher_weekly.py:112` `_normalize_upload_week(...)`
- `knowledge/teacher_weekly.py:512` `doc_week = _document_week(...)` (→ `NULL` for textbook)
- `knowledge/teacher_weekly.py:668-670` `TeacherUpload(week=week_val, kind=kind_norm, ...)` insert
- `server.py:940` route `POST /teacher/upload`; `server.py:983` `except ValueError → HTTPException(400)`
- Constraints: `database/migrations/004_teacher_features.sql:24` (week), next line (kind)
- Models: `database/models.py:303` `teacher_uploads.week` (NOT NULL); `:140` `aita_documents.week` (nullable)

**Supabase**
- Prod `uduxdniieeqbljtwocxy` — constraint state UNVERIFIED (prod query needs explicit auth).
- Test `hjevtxdtrkxjcaaexdxt` — `teacher_uploads` has only `status_check` (no week/kind checks).

---

## Tooling gotchas hit this session (save time next time)
- **Railway `get-logs` `filter`** is Loki/LogQL-style, **not** boolean `OR`. A filter like
  `"textbook OR error OR 500"` returned almost nothing. Use a single substring, a proper
  LogQL expression, or just pull unfiltered (`limit` up to 500) and scan.
- **GitHub MCP** intermittently returned `Authentication Failed: Bad credentials`
  (reconnect via `/mcp` fixed it). `gh` CLI is **not installed** on this machine.
- **Prod Supabase `execute_sql`** is blocked by the Claude Code auto-mode classifier
  unless the user explicitly authorizes the prod scope in-request.
- Local backend `.env` points at the **test** Supabase project, not prod.
