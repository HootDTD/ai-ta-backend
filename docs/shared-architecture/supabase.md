---
doc: shared/supabase
description: Supabase projects, schema map, pgvector/halfvec HNSW setup, and the timestamped CLI migration workflow
owns: []
related:
  - ai-ta-backend/_overview
  - ai-ta-backend/domain-data
  - shared/security
last_verified: 2026-07-16
stub: false
---

# Supabase

Supabase is the system of record: Postgres + pgvector for all app data, GoTrue for auth,
Storage for teacher upload assets. The Apollo subsystem's knowledge graph lives **outside**
Supabase on Neo4j Aura (instance `791f9ced`, configured via `NEO4J_*` env vars in
`ai-ta-backend/config/settings.py`) — only Apollo's relational state is in Supabase.

## Projects: prod vs test

| Project | Ref | Use |
|---|---|---|
| Prod | `uduxdniieeqbljtwocxy` | Live data; what Railway (`ApolloV3` branch; `Procfile` runs `web` uvicorn + `worker` teacher-upload worker) points at |
| Test | `hjevtxdtrkxjcaaexdxt` | Schema rehearsal + integration testing; migrations are mirrored here (see header of `database/migrations/022_enable_rls_stopgap.sql`). **Railway's `staging` environment backend/worker point HERE, not at prod** (verified 2026-06-11 by correlating staging worker/request timestamps with `pg_stat_activity`) |

Two MCP servers (`supabase` = prod, `supabase-test` = test) are wired into the workspace;
apply changes to test first, then prod.

**Known drift (2026-06-11)**: the test project's `teacher_uploads` carries only
`teacher_uploads_status_check` — the `week`/`kind` checks from migration 004 are absent
(its schema predates the SQL file or came from `Base.metadata.create_all`, which declares
no CHECK constraints). Migration `024_teacher_textbook.sql` re-adds both checks in their
relaxed form on whichever project it runs against, closing the drift.

## Schema overview (public schema, 17 tables + queue/decision tables)

All tables created by `ai-ta-backend/database/migrations/`; ORM models for the core set in
`database/models.py`.

- **Core RAG**: `aita_search_spaces` (one row per course — the tenant unit),
  `aita_documents` (uploaded materials, full text + `vector(3072)` embedding),
  `aita_chunks` (layout-aware chunks, the primary retrieval target — content, embedding,
  page/section/figure metadata).
- **Membership & teacher**: `course_memberships` (user↔course, role student/teacher; FK to
  `auth.users` done in raw SQL, migration 006), `course_invite_links` (008),
  `teacher_courses` (per-course week + retrieval weights, 004), `teacher_uploads` (004/007/024),
  `teacher_upload_jobs` (durable lease-based work queue for the worker process, 007).
- **Chat**: `chat_sessions` (incl. `memory_summary`, `topic_centroid_vector vector(3072)`
  from 015), `chat_turns` (011 added citations), `chat_session_snippets` +
  `chat_router_decisions` (RAG orchestrator, 015).
- **Apollo (tutoring)**: `apollo_sessions`, `apollo_kg_entries`, `apollo_messages`,
  `apollo_problem_attempts` (009/010/014/020), `apollo_student_progress` (013),
  `apollo_subjects` / `apollo_concepts` / `apollo_concept_problems` (018),
  `apollo_misconceptions` (019, embedded), `apollo_kg_negotiations` (021),
  `apollo_clarifications` (033), `apollo_grading_artifacts` (034 — canonical
  grading artifact, one immutable row per Done-click per grader role;
  LOCAL-Docker-verified only, not applied to any remote project yet; see
  `docs/architecture/apollo.md` persistence row for the full shape).
- **Reporting**: `ai_use_reports` (012) — the only table accessed via the anon-key REST
  client instead of SQLAlchemy (see shared/security).

## pgvector setup & the halfvec HNSW decision

- Embeddings: **text-embedding-3-large, 3072 dims** (`EMBEDDING_MODEL` / `EMBEDDING_DIM`).
  Stored full-precision as `vector(3072)` on `aita_documents.embedding`,
  `aita_chunks.embedding`, `chat_sessions.topic_centroid_vector`, and
  `apollo_misconceptions.description_embedding`.
- pgvector's HNSW limit is **2000 dims for `vector`** but **4000 for `halfvec`**, so the
  standing decision (root `CLAUDE.md`, migration 019 header) is: store `vector(3072)`,
  index via a **`halfvec(3072)` expression index** —
  `USING hnsw ((col::halfvec(3072)) halfvec_cosine_ops)`.
- **What actually exists on prod** (verified via `pg_indexes`, 2026-06-10):
  - `aita_chunks.idx_aita_chunks_embedding_hnsw` — halfvec expression HNSW. **Created
    ad-hoc on the DB; no migration file contains it.** Migration `001_create_schema.py`
    only creates plain-vector HNSW when `EMBEDDING_DIM <= 2000`, i.e. it skips indexing
    at 3072 — the halfvec index was added later, outside the numbered files.
  - `apollo_misconceptions_embedding_hnsw_idx` — from migration 019 (the only migration
    file that codifies the halfvec pattern).
  - `aita_documents.embedding` and `chat_sessions.topic_centroid_vector` have **no ANN
    index**.
- **Query-form caveat**: an expression index only matches queries using the same
  expression. `apollo/overseer/misconception_bank.py` does this correctly (casts both
  sides to `halfvec(3072)` around `<=>`). `retrieval/hybrid_search.py` orders by plain
  `embedding <=> :emb` (no halfvec cast) — that form does **not** match the halfvec
  expression index on `aita_chunks`; keep this in mind before assuming HNSW is being hit
  for chunk retrieval. Run `EXPLAIN` before/after touching either side.

## Migration workflow

- Supabase CLI **2.109.0** is pinned in `package.json`, checked by the local
  harness, and installed at that exact version in CI. `supabase/config.toml` is
  committed and contains local-only stack settings.
- `supabase/migrations/` is the only forward schema history. Create migrations
  with `supabase migration new <descriptive_name>`; filenames use a unique
  14-digit timestamp and are applied in ascending timestamp order. Never edit
  an applied migration; append a correction.
- `database/migrations/` is a read-only legacy archive through `047`. Its 48
  files include the historical duplicate `023`; normalized SHA-256 checksums
  make additions, deletions, and edits fail CI. The old Python/manual runner
  must not apply the active timestamped chain.
- `node scripts/db/reset-local.mjs` verifies history drift, requires the pinned
  CLI, starts the local Docker stack if needed, and runs `supabase db reset
  --local`. Reset applies the timestamped chain and then `supabase/seed.sql`.
  The harness exposes no linked or remote mode.
- CI performs the same drift check and empty-database reset as a required job.
  Remote history reconciliation and migration application remain explicit,
  separately reviewed human operations; agents never link or mutate a remote
  project.

## Access paths (which credential touches what)

| Path | Code | Credential |
|---|---|---|
| SQLAlchemy async (all app queries) | `database/session.py` (per-event-loop asyncpg engines) | `SUPABASE_DB_URL` (owner role — RLS-exempt) |
| PostgREST client (`ai_use_reports` only) | `vendors/supabase_client.py` | anon key (`SUPABASE_API_KEY`) |
| GoTrue token validation | `auth.py` | anon key |
| Storage (teacher upload assets) | `vendors/supabase_storage.py` | `SUPABASE_SERVICE_ROLE_KEY` |
| UIs (auth only — no table access) | `app/lib/auth.ts` in both UIs | `NEXT_PUBLIC_SUPABASE_ANON_KEY` |
