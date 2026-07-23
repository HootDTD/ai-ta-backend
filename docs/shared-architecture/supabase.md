---
doc: shared/supabase
description: Supabase projects, the app/internal target schema, pgvector/halfvec HNSW setup, and the timestamped CLI migration workflow
owns: []
related:
  - ai-ta-backend/_overview
  - ai-ta-backend/domain-data
  - shared/security
last_verified: 2026-07-22
stub: false
---

# Supabase

Supabase is the system of record: Postgres + pgvector for all app data, GoTrue for auth,
Storage for teacher upload assets. The Apollo subsystem's knowledge graph lives **outside**
Supabase on Neo4j Aura (instance `791f9ced`, configured via `NEO4J_*` env vars in
`ai-ta-backend/config/settings.py`; Aura is prod-only — staging runs Neo4j-degraded and
all staging/dev Neo4j testing is local Docker) — only Apollo's relational state is in
Supabase.

## Projects: prod vs test

| Project | Ref | Use |
|---|---|---|
| Prod | `uinkseewnxvumrxksnew` | Live data. Railway's **prod** environment (`hoot-ai-ta` project, deploys `main`; `Procfile` runs `web` uvicorn + `worker` teacher-upload worker) points here. The former prod ref `uduxdniieeqbljtwocxy` is DEAD. |
| Test | `hjevtxdtrkxjcaaexdxt` | Schema rehearsal + integration testing. **Railway's `staging` environment backend/worker point HERE, not at prod.** |

Two MCP servers (`supabase` = prod, `supabase-test` = test) are wired into the workspace;
rehearse changes on test first, then prod. Remote migration application is always a human
step — agents never link or mutate a remote project.

## Deployment state of the schema (read this first)

This branch replaces the legacy `public`-schema layout with the target `app`/`internal`
schema described below. **Both remote projects still run the legacy public schema until
the cutover runbook is executed against them**
(`docs/_archive/handoffs/2026-07-21-db-cutover-runbook.md`; test first, prod later inside
a maintenance window). The legacy layout is captured verbatim in the
`legacy_public_snapshot` migration, and the `copy_app_schema_v1` migration performs the
data copy with id remaps (chat session ids copied as-is; tutoring session ids offset by
+1,000,000 into `learning_activities`). Post-cutover, the legacy public schema is removed
by the human-run `scripts/db/remove_legacy_public_schema.sql` after a 14-day observation
window.

## Schema overview — target layout (18 `app` + 10 `internal` tables)

The authoritative chain is `supabase/migrations/`: `legacy_public_snapshot` →
`create_app_schema_v1` → `copy_app_schema_v1` → `retrieval_functions_v1`. ORM models live
in `database/models.py` and map to these tables. `app` is the policy-protected,
request-facing schema; `internal` is service-only (no `authenticated` table grants).

- **Core RAG**: `app.courses` (one row per course — the tenant unit; formerly
  `aita_search_spaces`), `app.documents` (uploaded materials, full text +
  `vector(3072)` embedding), `internal.document_chunks` (layout-aware chunks, the
  primary retrieval target — content, embedding + halfvec HNSW, FTS gin, page/section
  metadata; read via the hardened `retrieval_functions_v1` functions).
- **Membership & teacher**: `app.course_memberships` (user↔course, role
  student/teacher), `app.course_invites`, `app.uploads`, `internal.upload_jobs`
  (durable lease-based work queue for the worker process).
- **Chat + tutoring**: `app.learning_activities` (unified session supertype with a
  `modality` discriminator — chat and Apollo tutoring; tutoring state is nullable
  columns), `app.chat_messages` and `app.tutoring_messages` (typed children, each with
  denormalized `course_id` for initplan-safe RLS), `internal.chat_session_snippets` +
  `internal.chat_routing_decisions` (RAG orchestrator).
- **Curriculum**: `app.concepts` (subjects folded in as `subject_slug` +
  `subject_display_name`), `app.problems` (promoted typed columns; bigint ids are
  internal, public problem codes are API-facing), `app.provisioning_runs` (merged
  authored_sets + generation_runs, `kind` discriminator).
- **Learner & grading**: `app.learner_entities` + `internal.entity_prerequisites`,
  `app.learner_state`, `app.mastery_events`, `app.student_progress` (PK
  `(user_id, course_id)` — progress is per course), `app.problem_attempts`,
  `app.question_opportunities`, `internal.grading_runs` (artifacts-only canonical
  grading record; misconceptions nest in `grader_payload`).
- **Ingest**: `internal.content_ingest_runs`, `internal.content_ingest_errors`,
  `internal.dedup_decisions`, `internal.ingest_page_evidence`.
- **Reporting**: `app.ai_usage_reports` — accessed via a typed SQLAlchemy repository
  with owner-scoped routes (the legacy anon-key PostgREST path was deleted; cross-user
  reads 404 like missing rows).

Dropped from the target entirely: negotiations (A6), subjects as a table (A8),
graph-comparison runs/findings (A7 — roadmap abandoned), and the misconception
bank/columns (the served grader is transcript grader + topic score).

## pgvector setup & the halfvec HNSW decision

- Embeddings: **text-embedding-3-large, 3072 dims** (`EMBEDDING_MODEL` /
  `EMBEDDING_DIM`). Exactly two vector columns survive in the target:
  `app.documents.embedding` and `internal.document_chunks.embedding`, both
  `extensions.vector(3072)` (the extension is relocated to the `extensions` schema by
  `create_app_schema_v1`).
- pgvector's HNSW limit is **2000 dims for `vector`** but **4000 for `halfvec`**, so the
  standing decision holds: store `vector(3072)`, index via a **`halfvec(3072)`
  expression index**. The target codifies it in-migration:
  `document_chunks__embedding_halfvec_hnsw__idx` on `internal.document_chunks`
  (`USING hnsw ((embedding::extensions.halfvec(3072)) extensions.halfvec_cosine_ops)`).
  `app.documents.embedding` is intentionally unindexed (no ANN query path reads it).
- The legacy query-form caveat (plain `embedding <=> :emb` not matching the expression
  index) is **fixed in the target**: the `retrieval_functions_v1` functions cast **both**
  sides to `extensions.halfvec(3072)`, so the HNSW index is actually engaged. If you add
  a new ANN query, keep both-side casts and verify with `EXPLAIN`.

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
- Human-run operational scripts live in `scripts/db/` deliberately outside the
  auto-applied chain: `reconcile_copy.sql`, `rollback_reverse_copy.sql`,
  `remove_legacy_public_schema.sql`, `drop_duplicate_indexes.sql`,
  `unused_index_review.sql`.

## Access paths (which credential touches what)

| Path | Code | Credential |
|---|---|---|
| SQLAlchemy async (all app queries, incl. `app.ai_usage_reports`) | `database/session.py` (per-event-loop asyncpg engines) | `SUPABASE_DB_URL` (Supabase `postgres` role — carries BYPASSRLS, so RLS-exempt today; app tables are FORCE-RLS, so enforcement begins the moment a non-BYPASSRLS role is used — see shared/security, DB-08b) |
| GoTrue token validation | `auth.py` | anon key (`SUPABASE_API_KEY`) |
| Storage (teacher upload assets) | `vendors/supabase_storage.py` | `SUPABASE_SERVICE_ROLE_KEY` |
| UIs (auth only — no table access) | `app/lib/auth.ts` in both UIs | `NEXT_PUBLIC_SUPABASE_ANON_KEY` |

There is no PostgREST data path anymore: the anon-key REST client was deleted with the
reports redesign, and `anon` has zero privileges on the `app`/`internal` schemas.
