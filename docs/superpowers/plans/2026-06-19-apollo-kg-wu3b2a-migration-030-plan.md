# Plan: WU-3B2a — Migration 030 + ORM + Tier-2 selection gate

**Goal:** Ship migration `030_apollo_autoprovisioning.sql` (the auto-provisioning schema substrate) + its ORM mirror + the one-predicate Tier-2 gate on `list_problems_for_concept`, fully verified on local pgvector Testcontainers.
**Architecture:** ALTER `apollo_concept_problems` (+5 cols) & `apollo_kg_entities` (+1 col) with two in-migration backfills; 5 NEW course-scoped observability/queue tables (RLS-stopgap'd); ORM additions; a single `tier == 2` WHERE clause that gates both selectors.
**Tech stack:** Supabase Postgres 16 + pgvector (HNSW); raw-SQL numbered migrations (`database/migrations/`, next-free = 030); SQLAlchemy async + asyncpg ORM; pytest + Testcontainers `pgvector/pgvector:pg16` real-PG harness. Compare-branch for diff-cover: `feat/apollo-kg-wu6a3-selection-wiring`. Branch: `feat/apollo-kg-wu3b2a-migration-030` (already checked out — do NOT switch).

---
provides:
  - database/migrations/030_apollo_autoprovisioning.sql
  - apollo_concept_problems.tier / .solution_source / .provenance / .quarantined_at / .search_space_id
  - apollo_kg_entities.scope_summary
  - apollo_ingest_runs / apollo_provisioning_jobs / apollo_rejected_problems / apollo_dedup_decisions / apollo_ingest_errors (tables + ORM)
  - ORM: ConceptProblem(+5), KGEntity(+1), IngestRun, RejectedProblem, DedupDecision, IngestError, ProvisioningJob
  - list_problems_for_concept tier==2 gate (gates select_problem + select_problem_personalized)
consumes:
  - apollo_concept_problems (018), apollo_concepts (018), apollo_subjects (018+026), apollo_kg_entities (026), aita_search_spaces (app metadata)
depends_on:
  - feat/apollo-kg-wu6a3-selection-wiring (#49, tip a869dc9 — diff-cover compare branch + the just-shipped personalized selector)
---

## Overview

WU-3B2a lays the DB substrate for §8B materials→Apollo auto-provisioning. It is the FIRST of eight stacked sub-units and the only real-PG/Testcontainers unit. It writes **no pipeline logic, no LLM code** — just:

1. **Migration `030_apollo_autoprovisioning.sql`** — the verbatim adjudicated DDL: 5 new columns on `apollo_concept_problems` (`tier`, `solution_source`, `provenance`, `quarantined_at`, `search_space_id`), 1 new column on `apollo_kg_entities` (`scope_summary`), 5 new tables (`apollo_ingest_runs`, `apollo_provisioning_jobs`, `apollo_rejected_problems`, `apollo_dedup_decisions`, `apollo_ingest_errors`), two in-migration backfills, an RLS stopgap on the 5 new tables, and a manual-rollback comment block.
2. **ORM mirror** in `apollo/persistence/models.py` — 6 column adds + 5 new model classes, following the repo's `_JSONType` / `BigInteger().with_variant(Integer(),"sqlite")` / `server_default` / NO-DB-CHECK-in-ORM conventions.
3. **The Tier-2 selection gate** — one `ConceptProblem.tier == 2` predicate on `list_problems_for_concept`, which (verified) is the SOLE chokepoint funnelling both `select_problem` and `select_problem_personalized`.

The two load-bearing safety behaviors this unit owns:
- **The tier=2 backfill** — existing §8-seeded bernoulli rows MUST flip to `tier=2, solution_source='authored'` inside the migration, or the live teachable pool vanishes the instant the tier filter ships (highest blast radius).
- **The tier filter** — Tier-1 (auto-provisioned inventory) and quarantined problems MUST NOT leak to students; only Tier-2 is selectable.

Both are MUTATION-PROVED by tests that RED when the safety line is reverted.

## Scope & out-of-scope boundaries

**IN scope (this unit):**
- `database/migrations/030_apollo_autoprovisioning.sql` (NEW — owned here).
- `apollo/persistence/models.py` (EDIT — column adds + 5 new classes).
- `apollo/overseer/problem_selector.py` (EDIT — ~1 line, `tier == 2` predicate).
- `apollo/subjects/tests/_curriculum_fixtures.py` (EDIT — `seed_problems` defaults `tier=2`; blast-radius fix, see that section).
- `tests/database/test_apollo_autoprovisioning_migration.py` (NEW — adapts the 026 harness).
- `apollo/overseer/tests/test_problem_selector.py` (EDIT — assert Tier-1 excluded / Tier-2 included).
- `docs/architecture/domain-data.md` + `docs/architecture/apollo.md` (owner-doc drift, same commit).

**OUT of scope (held firmly — these belong to LATER sub-units):**
- The `quarantined_at IS NULL` filter clause → **WU-3B2h** (the column EXISTS in 030, but the filter is NOT added now).
- Any pipeline / LLM / scrape / generate / pairing / mint / dedup logic → 3B2b+.
- The 8-gate lint, dedup ladder, metered LLM client, worker shell, orchestrator, trigger enqueue, quarantine statistic → 3B2b–3B2h.
- The `knowledge/teacher_weekly.py` enqueue edit → 3B2g.
- `APOLLO_AUTOPROVISION_ENABLED` flag → introduced by a later unit (confirmed net-new, does not exist yet).
- Any new package (ADJUDICATION #8 = none; if a step needs one → BLOCK + escalate).
- Any remote-DB apply (test or prod). 030 runs on LOCAL Testcontainers ONLY.

## Structural prep (from neighborhood scan)

Scanned the change path (one ring out): `apollo_concept_problems`, `apollo_kg_entities`, `list_problems_for_concept`, and the migration-test harness.

- **`list_problems_for_concept`** (`problem_selector.py:34-48`): 15 lines, single responsibility (load + validate rows). Two consumers (`select_problem`, `select_problem_personalized`) — both in-file, both funnel through it. **No tangle, no sprawl.** Adding one predicate keeps it well under thresholds. Clean.
- **`apollo_concept_problems` fan-in**: consumers are the selector chain + `_curriculum_fixtures.seed_problems` + the WU-3D/6A test suites. Below the 10-consumer coupling-hub threshold; the only coupling concern is the seed-helper default (addressed explicitly in "Test seed-helper change").
- **`apollo_kg_entities`**: adding a nullable column only; no consumer reads `scope_summary` in this unit. Clean.
- **Access-policy sprawl**: the 5 new tables get RLS-enabled with ZERO policies (the established 022/026 stopgap). No policy sprawl; default-deny is intentional.

**Verdict: neighborhood is clean — no structural prep required.** No artifact in the change path exceeds the WMC / fan-in / tangle / policy-sprawl thresholds. (Structural-prep budget = 0% of plan steps.)

## Prior art (existing migrations)

- **`database/migrations/026_apollo_learner_model.sql`** — the canonical convention to mirror: `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` (`:185`); `search_space_id INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE` (`:188`); `JSONB ... DEFAULT '{}'::jsonb` / `'[]'::jsonb`; the §7 RLS stopgap `ENABLE ROW LEVEL SECURITY` with NO policies (`:257-269`); the manual-rollback comment block (`:273-286`); the two-step nullable-add → backfill → tighten pattern (`:34-68`, the pattern `apollo_concept_problems.search_space_id` partially follows — nullable + backfill here, tighten deferred).
- **`database/migrations/018_apollo_concept_registry.sql:77-90`** — the base `apollo_concept_problems` table (`id` BIGINT PK, `concept_id` BIGINT FK, `problem_code`/`difficulty` TEXT, `payload` JSONB, `UNIQUE(concept_id, problem_code)`, index `(concept_id, difficulty)`); 030 ALTERs this table and adds an index `(concept_id, tier)` paralleling the existing `(concept_id, difficulty)` index.
- **`database/models.py:358` `TeacherUploadJob`** — the lease-shape template (`lease_owner` / `lease_expires_at` / `attempt_count` / `state` with a CHECK) the `apollo_provisioning_jobs` queue + the `ProvisioningJob` ORM mirror.
- **`apollo/persistence/models.py:280-294`** — the `server_default=text("...")` + `default=...` convention for adding a non-null column safely (so raw-SQL/legacy/migration inserts see the default and ORM inserts also do). `tier` follows this exactly.
- **`tests/database/test_apollo_learner_model_migration.py`** — the real-PG migration-test harness (the `_TOUCHES_TARGETS` content-scoped chain selector `:54-57`, `_EXCLUDE_FROM_CHAIN` `:65-68`, `_STUB_DDL` `:47-51`, the `_expect_violation` savepoint helper `:144-165`, the `pre026_factory` populated-pre-migration backfill harness `:377-428`). 030's test ADAPTS this — see the test list.

## Schema changes (migration 030 — verbatim from the adjudicated split proposal)

Create `database/migrations/030_apollo_autoprovisioning.sql` with EXACTLY this content (header comment + the pinned DDL). The header mirrors 026's deploy-reconciliation block. Every statement is commented with WHY. ADJUDICATION #3 rules: `search_space_id` is NOT NULL on the 5 new tables, NULLABLE on `apollo_concept_problems`.

```sql
-- 030_apollo_autoprovisioning.sql
-- §8B materials -> Apollo auto-provisioning substrate (WU-3B2a).
-- Spec: docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md §8B.
-- Adjudicated DDL: docs/superpowers/plans/2026-06-19-apollo-kg-wu3b2-split-proposal.md
--   '### WU-3B2a' + 'ORCHESTRATOR ADJUDICATION (2026-06-19)' decisions #3/#6/#11.
--
-- This unit ships SCHEMA + ORM + the Tier-2 selection gate ONLY. No pipeline/LLM logic.
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 029_chat_turn_keywords.sql; 030 is the next free number.
--   * This file is applied to LOCAL Docker Postgres ONLY by feller agents. Rehearsal on the
--     TEST Supabase project then prod is a human/CI step (see plan "Deploy handoff").
--
-- TWO BACKFILLS (both inside this migration):
--   (a) denormalize search_space_id onto apollo_concept_problems via the concept->subject join.
--   (b) CRITICAL — flip every existing (seeded) tier=1 row to tier=2/'authored' so the live
--       bernoulli pool stays teachable the instant the tier filter ships. Highest blast radius.

BEGIN;
-- 1. apollo_concept_problems: tier/solution_source/provenance/quarantine + denormalized scope.
ALTER TABLE apollo_concept_problems
  ADD COLUMN IF NOT EXISTS tier            SMALLINT NOT NULL DEFAULT 1
      CHECK (tier IN (1, 2)),                              -- 1=inventory (not teachable), 2=teachable
  ADD COLUMN IF NOT EXISTS solution_source TEXT
      CHECK (solution_source IS NULL OR solution_source IN ('extracted','generated','authored')),
  ADD COLUMN IF NOT EXISTS provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {document_id,page,chunk_content_hash}
  ADD COLUMN IF NOT EXISTS quarantined_at  TIMESTAMPTZ,                          -- §8B.3c anomaly quarantine (NULL = live); FILTER added in WU-3B2h, NOT here
  ADD COLUMN IF NOT EXISTS search_space_id INTEGER;                             -- denormalized course scope (ADJUDICATION #3: NULLABLE here)
-- backfill (a): denormalize the course scope from the concept->subject chain.
UPDATE apollo_concept_problems p SET search_space_id = sub.search_space_id
  FROM apollo_concepts c JOIN apollo_subjects sub ON sub.id = c.subject_id
  WHERE p.concept_id = c.id AND p.search_space_id IS NULL;
-- backfill (b) CRITICAL: existing §8-seeded rows ARE teachable -> tier=2, solution_source='authored'.
UPDATE apollo_concept_problems SET tier = 2, solution_source = 'authored' WHERE tier = 1;
CREATE INDEX IF NOT EXISTS apollo_concept_problems_concept_tier_idx
  ON apollo_concept_problems(concept_id, tier);

-- 1b. apollo_kg_entities: scope_summary text (the dedup embedding SOURCE, embedded on the fly; 3B2c).
ALTER TABLE apollo_kg_entities
  ADD COLUMN IF NOT EXISTS scope_summary TEXT;  -- nullable; authored at mint; NO persisted vector (no pgvector migration)

-- 2. apollo_ingest_runs — per-doc counts + LLM call/token/cost aggregates + content_hash + status.
CREATE TABLE IF NOT EXISTS apollo_ingest_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  content_hash      TEXT,                 -- document content hash; an unchanged re-upload short-circuits (3B2g)
  status            TEXT    NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
  n_questions_scraped INTEGER NOT NULL DEFAULT 0,
  n_promoted        INTEGER NOT NULL DEFAULT 0,
  n_rejected        INTEGER NOT NULL DEFAULT 0,
  n_dedup_merged    INTEGER NOT NULL DEFAULT 0,
  llm_calls         INTEGER NOT NULL DEFAULT 0,
  llm_tokens_in     BIGINT  NOT NULL DEFAULT 0,
  llm_tokens_out    BIGINT  NOT NULL DEFAULT 0,
  llm_cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_space_doc_idx ON apollo_ingest_runs(search_space_id, document_id);
-- a succeeded run per (document_id, content_hash) lets enqueue short-circuit an unchanged re-upload:
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_doc_hash_idx ON apollo_ingest_runs(document_id, content_hash);

-- 3. apollo_provisioning_jobs — the SKIP LOCKED work queue (mirrors TeacherUploadJob lease shape).
CREATE TABLE IF NOT EXISTS apollo_provisioning_jobs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  state             TEXT    NOT NULL DEFAULT 'pending'
      CHECK (state IN ('pending','running','completed','failed')),
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE SET NULL,
  lease_owner       TEXT,
  lease_expires_at  TIMESTAMPTZ,
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- idempotent enqueue: at most one OPEN job per document (partial-unique-index idiom).
-- JOB-level dedup; the (document_id, chunk_content_hash) no-op is a SEPARATE intra-job guarantee (3B2g).
CREATE UNIQUE INDEX IF NOT EXISTS apollo_provisioning_jobs_open_uniq
  ON apollo_provisioning_jobs(document_id) WHERE state IN ('pending','running');

-- 4. apollo_rejected_problems — a gate failure + diagnostic + the rejected payload.
CREATE TABLE IF NOT EXISTS apollo_rejected_problems (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,  -- NULL if rejected pre-tag
  failed_gate       SMALLINT,            -- 1..8 (NULL if rejected at the pairing gate, not a lint gate)
  rejected_stage    TEXT    NOT NULL,    -- 'pairing_gate' | 'promotion_lint'
  diagnostic        TEXT    NOT NULL,
  payload           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_rejected_problems_run_idx ON apollo_rejected_problems(ingest_run_id);

-- 5. apollo_dedup_decisions — method + similarity + verdict for every dedup resolution.
CREATE TABLE IF NOT EXISTS apollo_dedup_decisions (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,
  candidate_key     TEXT    NOT NULL,
  method            TEXT    NOT NULL CHECK (method IN ('slug','embedding','llm_judge')),
  similarity        REAL,                -- NULL for the slug exact-match tier
  verdict           TEXT    NOT NULL CHECK (verdict IN ('merged','distinct')),
  matched_entity_id BIGINT  REFERENCES apollo_kg_entities(id) ON DELETE SET NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_dedup_decisions_run_idx ON apollo_dedup_decisions(ingest_run_id);

-- 6. apollo_ingest_errors — stage + class + context for any non-terminal pipeline error.
CREATE TABLE IF NOT EXISTS apollo_ingest_errors (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  stage             TEXT    NOT NULL,    -- scrape|find_or_generate|pairing|tag_mint|dedup|promotion
  error_class       TEXT    NOT NULL,
  context           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_errors_run_idx ON apollo_ingest_errors(ingest_run_id);

-- 7. RLS stopgap (mirror 026 §7 / migration 022): default-deny to PostgREST on the 5 new tables.
ALTER TABLE apollo_ingest_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_provisioning_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_rejected_problems  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_dedup_decisions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_ingest_errors      ENABLE ROW LEVEL SECURITY;
COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; DROP TABLE IF EXISTS apollo_ingest_errors, apollo_dedup_decisions, apollo_rejected_problems,
--   apollo_provisioning_jobs, apollo_ingest_runs;
-- ALTER TABLE apollo_kg_entities DROP COLUMN IF EXISTS scope_summary;
-- DROP INDEX IF EXISTS apollo_concept_problems_concept_tier_idx;
-- ALTER TABLE apollo_concept_problems DROP COLUMN IF EXISTS search_space_id, DROP COLUMN IF EXISTS quarantined_at,
--   DROP COLUMN IF EXISTS provenance, DROP COLUMN IF EXISTS solution_source, DROP COLUMN IF EXISTS tier; COMMIT;
```

**Do not deviate from this DDL.** It is the adjudicated, file:line-verified contract. The only freedom: header-comment wording and whitespace.

## Access control (RLS stopgap + course-isolation predicate)

Postgres engine → row-level security is the native mechanism. This unit follows the established 022/026 **RLS stopgap**: enable RLS with NO policies on the 5 new public tables, so the anon/public PostgREST key is default-denied while the owner-connection backend (which bypasses RLS) reaches them. **Tenant isolation is enforced at two points:**

1. **DB structural scope:** every new table carries `search_space_id INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE` — a course delete cascades its provisioning rows away, and every app query filters by `search_space_id` (the §1.4 isolation invariant). The denormalized `apollo_concept_problems.search_space_id` gives the future Tier-2 course-scoped index a direct column (NULLABLE in v1; tightened in a later two-step migration).
2. **App-layer enforcement point:** `auth.py` (Supabase JWT + course-membership) + the `search_space_id` predicate carried through the apollo runtime — the same enforcement point migration 026's RLS-stopgap comment names. No new policy is added here.

**Positive test (authorized):** a run/job/rejected/dedup/error row for course-A is returned when querying `WHERE search_space_id = <A>` (covered by `test_*_scoped_select_returns_rows`).
**Negative test (different tenant):** the same query with course-B's id returns ZERO rows for course-A's data (covered by `test_*_other_tenant_returns_zero_rows`), and `DELETE FROM aita_search_spaces WHERE id=<A>` cascades only course-A's provisioning rows, leaving course-B intact (covered by `test_course_delete_cascades_only_provisioning_rows`).

## Migration plan (single-phase; two in-migration backfills)

This is a **single-phase** migration — it is additive (new nullable/defaulted columns + new tables) and its two backfills run inside the same transaction. No 2-phase destructive change is required because:
- `tier` adds with `NOT NULL DEFAULT 1` then is immediately backfilled to 2 for seeded rows in the SAME migration — safe on a populated table (the DEFAULT covers the ADD; the UPDATE is a small bounded backfill on the curriculum table, not user-event-scale).
- `search_space_id` on `apollo_concept_problems` lands NULLABLE (ADJUDICATION #3) and is backfilled; tightening to NOT NULL is a deferred two-step migration owned by a LATER unit, explicitly out of scope here.
- All other columns are nullable or defaulted; all new tables are empty.

- [ ] **Migration file:** `database/migrations/030_apollo_autoprovisioning.sql` — contents = the verbatim DDL above.
  - Verify (fresh chain): `pytest tests/database/test_apollo_autoprovisioning_migration.py -q` runs the chain+030 against a fresh Testcontainers DB; expect all tests green (NOT skipped).
  - Verify (backfill (b)): the `pre030_factory` test seeds a pre-030 ConceptProblem row (which defaults `tier=1` only because the column doesn't exist pre-030 — seeded as a plain 018-shape row), applies 030, asserts the row is now `tier=2, solution_source='authored'`.
  - Verify (backfill (a)): the same test asserts `search_space_id` is populated via the concept→subject join and equals the seeded course id.

**Lock duration:** the `ALTER TABLE apollo_concept_problems ADD COLUMN ... DEFAULT` takes a brief ACCESS EXCLUSIVE lock; on PG11+ a constant DEFAULT add is a metadata-only operation (no table rewrite), so the lock is short even on a large table. The `(concept_id, tier)` index build is on the curriculum table (small). Stated for the deploy-handoff rehearsal — NOT executed by feller.

## ORM changes

Edit `apollo/persistence/models.py`. Follow the existing conventions EXACTLY (verified against the file): `_JSONType` for JSONB, `BigInteger().with_variant(Integer(),"sqlite")` for identity PKs, `Integer` + `ForeignKey("aita_search_spaces.id", ondelete="CASCADE")` for course scope, `server_default=text("...")` + `default=...` for new non-null columns, and **NO DB CHECK constraints declared in the ORM** (CHECKs live in SQL only — `tier`/`solution_source`/`status`/`state`/`method`/`verdict` CHECKs are NOT mirrored as `CheckConstraint`).

**A. `ConceptProblem` (`:158-169`) — add 5 columns** (after `payload`, before `created_at`):
```python
# WU-3B2a / migration 030 (§8B auto-provisioning). tier gates selectability:
# 1 = inventory (auto-scraped, NOT teachable), 2 = teachable. The DB CHECK
# (tier IN (1,2)) is the authority; the ORM declares none (repo convention).
# server_default so raw-SQL/migration/legacy inserts see 1; default=1 for ORM.
tier = Column(SmallInteger, nullable=False, server_default=text("1"), default=1)
solution_source = Column(Text, nullable=True)  # extracted|generated|authored (CHECK in SQL)
provenance = Column(_JSONType, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
quarantined_at = Column(TIMESTAMP(timezone=True), nullable=True)  # WU-3B2h filter; NULL = live
# Denormalized course scope (migration 030). NULLABLE in v1 (backfilled in-migration;
# a later two-step migration tightens it). No ORM FK declared to keep create_all/SQLite
# parity simple — the migration SQL is the FK authority where it exists.
search_space_id = Column(Integer, nullable=True)
```
- Import `SmallInteger` from `sqlalchemy` (add to the existing import block at `:16-29`; `text` is already imported).
- Note on `provenance` server_default: use `text("'{}'::jsonb")` to match the migration; SQLite `create_all` ignores the PG cast harmlessly (the variant `_JSONType` resolves the column type; `server_default` is emitted only where the dialect supports it — consistent with how 026's JSONB defaults are handled). If a SQLite `create_all` rejects the `::jsonb` server_default literal in CI, fall back to `default=dict` only and drop `server_default` for `provenance` (the real-PG schema comes from the migration, not `create_all`).

**B. `KGEntity` (`:362-389`) — add 1 column** (after `aliases`, before `created_at`):
```python
# WU-3B2a / migration 030: the dedup ladder's embedding SOURCE (authored at
# mint from display_name + canonical symbols; embedded ON THE FLY by 3B2c).
# Nullable; NO persisted vector column (no pgvector-on-entities migration).
scope_summary = Column(Text, nullable=True)
```

**C. 5 NEW model classes** — append after `GraphComparisonFinding` (end of file). Each mirrors 026's runs/findings ORM shape (identity PK, `Integer` search_space_id FK, `_JSONType` JSONB cols, NO DB CHECK in ORM, timestamps via `default=lambda: datetime.now(UTC)`). Public class names + `__tablename__`:

- `IngestRun` → `apollo_ingest_runs`: `id`, `search_space_id` (FK CASCADE), `document_id` (BigInteger), `content_hash` (Text, nullable), `status` (Text, server_default `'queued'`, default `'queued'`), `n_questions_scraped`/`n_promoted`/`n_rejected`/`n_dedup_merged`/`llm_calls` (Integer, server_default `0`, default `0`), `llm_tokens_in`/`llm_tokens_out` (BigInteger, server_default `0`, default `0`), `llm_cost_usd` (`Numeric(12, 6)`, server_default `text("0")`, default `0`), `started_at`/`finished_at` (TIMESTAMP nullable), `created_at`. Import `Numeric` from `sqlalchemy`.
- `ProvisioningJob` → `apollo_provisioning_jobs`: `id`, `search_space_id` (FK CASCADE), `document_id` (BigInteger), `state` (Text, server_default `'pending'`, default `'pending'`), `ingest_run_id` (BigInteger FK `apollo_ingest_runs.id` SET NULL, nullable), `lease_owner` (Text nullable), `lease_expires_at` (TIMESTAMP nullable), `attempt_count` (Integer, server_default `0`, default `0`), `last_error` (Text nullable), `created_at`, `updated_at`. (The partial-unique-index is migration-only — NOT declared in the ORM, same as how 028's partial index is migration-only.)
- `RejectedProblem` → `apollo_rejected_problems`: `id`, `ingest_run_id` (BigInteger FK CASCADE, nullable), `search_space_id` (FK CASCADE), `concept_id` (BigInteger FK `apollo_concepts.id` SET NULL, nullable), `failed_gate` (SmallInteger nullable), `rejected_stage` (Text), `diagnostic` (Text), `payload` (`_JSONType`, default dict), `created_at`.
- `DedupDecision` → `apollo_dedup_decisions`: `id`, `ingest_run_id` (FK CASCADE nullable), `search_space_id` (FK CASCADE), `concept_id` (FK SET NULL nullable), `candidate_key` (Text), `method` (Text), `similarity` (Float/REAL nullable), `verdict` (Text), `matched_entity_id` (BigInteger FK `apollo_kg_entities.id` SET NULL nullable), `created_at`.
- `IngestError` → `apollo_ingest_errors`: `id`, `ingest_run_id` (FK CASCADE nullable), `search_space_id` (FK CASCADE), `stage` (Text), `error_class` (Text), `context` (`_JSONType`, default dict), `created_at`.

Each class gets a one-paragraph docstring naming migration 030 + the §8B role + the "NO DB CHECK in ORM; SQL is the authority" note (mirroring the existing 026 class docstrings).

**ORM/migration parity guard:** the `db_session` fixture builds the schema from `Base.metadata.create_all`, so these ORM additions are what the selector + curriculum tests run against; the migration test builds from the raw SQL. The two MUST agree on column names/types/nullability/defaults. The test list includes an explicit parity assertion (see `test_orm_concept_problem_has_tier_columns`).

## Selection-gate change

Edit `apollo/overseer/problem_selector.py::list_problems_for_concept` (`:38-46`) — add ONE predicate to the WHERE. **No signature change. No edit to `select_problem` or `select_problem_personalized`** (both funnel through this function — verified):

```python
rows = (
    (
        await db.execute(
            select(ConceptProblem.payload).where(
                ConceptProblem.concept_id == concept_id,
                ConceptProblem.tier == 2,
            )
        )
    )
    .scalars()
    .all()
)
```
Update the function docstring: note that only Tier-2 (teachable) problems are returned; Tier-1 (auto-provisioned inventory) is excluded. Add a comment that the `quarantined_at IS NULL` clause is added by WU-3B2h (do NOT add it now — the column exists but the filter is 3B2h's).

## Test seed-helper change (blast-radius fix)

**This is mandatory and load-bearing.** `apollo/subjects/tests/_curriculum_fixtures.py::seed_problems` (`:138-158`) is used by ~14 downstream test files (verified via grep: `test_next_db.py`, `test_lifecycle_db.py`, `test_personalization_select_route_postgres.py`, `test_done_concept_db.py`, `test_curriculum_db.py`, etc.). These tests seed a problem then call `list_problems_for_concept` / `select_problem` and expect the problem back. After the tier filter ships, a row with the DB default `tier=1` is INVISIBLE → those tests break.

The seeded curriculum problems are the §8-authored bernoulli set — they ARE teachable, exactly the rows the migration's backfill (b) flips to `tier=2`. So `seed_problems` MUST insert `tier=2` by default to mirror the post-migration reality:

```python
async def seed_problems(
    db: AsyncSession,
    *,
    concept_id: int,
    payloads: Sequence[dict[str, Any]],
    tier: int = 2,  # WU-3B2a: seeded curriculum problems are teachable (Tier-2),
                    # mirroring migration 030's backfill of existing §8-seeded rows.
) -> list[str]:
    ...
    ConceptProblem(
        concept_id=concept_id,
        problem_code=code,
        difficulty=payload["difficulty"],
        payload=payload,
        tier=tier,
    )
```
The `tier` parameter is added (default 2) so the NEW selector tests can seed an explicit `tier=1` row to prove exclusion. `seed_course` passes through unchanged (it delegates to `seed_problems`, which now defaults to 2). This keeps the entire existing real-PG suite green while letting the new tests exercise both tiers.

## TDD test list (write tests FIRST)

TDD order: write each test, run it RED (the migration/ORM/selector change not yet present), then implement, then GREEN. No skip/xfail, no assert-nothing. All DB tests carry `pytestmark = pytest.mark.integration` and run against the real pgvector container (green-not-skipped — the verify REAL-INFRA gate WILL fire because changed files live under `database/migrations/` + `tests/database/`). Run with `.venv/Scripts/python.exe -m pytest`. Mocking: this unit has NO LLM/network — there is nothing to mock; everything is real-PG DDL + pure ORM/SQL. (No `patch`, no fake clients — if a test seems to need one, it is out of scope for 3B2a.)

### File 1 (NEW): `tests/database/test_apollo_autoprovisioning_migration.py` — adapts the 026 harness

**Harness adaptation (re-derive the chain selector — do NOT copy 026's 1:1):**
- `_STUB_DDL`: keep `auth.users` + `aita_search_spaces` stubs (030's new tables FK `aita_search_spaces`).
- `_TOUCHES_TARGETS`: 030 ALTERs `apollo_concept_problems` + `apollo_kg_entities`, and FKs the 5 new tables to `aita_search_spaces` + `apollo_concepts` + `apollo_kg_entities`. The chain must therefore include every migration that CREATEs/ALTERs `apollo_(subjects|concepts|concept_problems|kg_entities)`. Concretely the chain must contain **018** (creates concepts/concept_problems) and **026** (creates kg_entities AND adds `apollo_subjects.search_space_id` that backfill (a)'s join reads). Regex: `(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+apollo_(subjects|concepts|concept_problems|kg_entities)\b`.
- `_EXCLUDE_FROM_CHAIN`: exclude `030_apollo_autoprovisioning.sql` itself (applied separately, LAST) plus any migration that matches the regex but depends on columns 030 needs ordered after it — verify by listing the matched chain and confirming 018 precedes 026 precedes 030. Exclude `028_apollo_learner_janitor.sql` (matches nothing here but keep the guard pattern). The executor MUST print the resolved `_chain_migrations()` list in a one-time assertion test (`test_chain_order_018_before_026_before_030`) so the ordering is proven, not assumed.
- Provide a `_migrated_dsn` session fixture (chain + 030) + a `mig_conn` per-test transaction-rollback connection + the `_expect_violation` savepoint helper (copy verbatim from 026's harness) + a `pre030_factory` (build chain WITHOUT 030, seed, then apply 030 — for the backfill tests).

**Schema-shape tests:**
1. `test_all_five_new_tables_created` — `to_regclass('public.apollo_ingest_runs'|...|apollo_ingest_errors)` all non-null after chain+030. Asserts the 5 tables exist.
2. `test_concept_problems_new_columns_exist` — `information_schema.columns` shows `tier`/`solution_source`/`provenance`/`quarantined_at`/`search_space_id` on `apollo_concept_problems` with the expected data types (`smallint`, `text`, `jsonb`, `timestamp with time zone`, `integer`) and nullability (`tier` NOT NULL; `search_space_id`/`solution_source`/`quarantined_at` nullable; `provenance` NOT NULL).
3. `test_kg_entities_scope_summary_exists` — `scope_summary` is a nullable `text` column on `apollo_kg_entities`.
4. `test_concept_tier_index_exists` — `pg_indexes` contains `apollo_concept_problems_concept_tier_idx` on `apollo_concept_problems`.
5. `test_ingest_runs_doc_hash_index_exists` — `pg_indexes` contains `apollo_ingest_runs_doc_hash_idx` (the content_hash short-circuit index) AND `apollo_ingest_runs_space_doc_idx`.

**CHECK-constraint tests (one positive + one negative each; use `_expect_violation` for the negative):**
6. `test_tier_check_accepts_1_and_2_rejects_others` — insert a concept_problem with `tier=1` ok, `tier=2` ok, `tier=3` → `CheckViolationError`, `tier=0` → `CheckViolationError`.
7. `test_solution_source_check` — `NULL` ok, `'extracted'`/`'generated'`/`'authored'` ok, `'invented'` → `CheckViolationError`.
8. `test_ingest_runs_status_check` — `'queued'`/`'running'`/`'succeeded'`/`'failed'` ok; `'pending'` → `CheckViolationError` (status uses queued, NOT pending — a deliberate distinct vocabulary from the job `state`).
9. `test_provisioning_jobs_state_check` — `'pending'`/`'running'`/`'completed'`/`'failed'` ok; `'queued'` → `CheckViolationError`.
10. `test_dedup_method_check` — `'slug'`/`'embedding'`/`'llm_judge'` ok; `'fuzzy'` → `CheckViolationError`.
11. `test_dedup_verdict_check` — `'merged'`/`'distinct'` ok; `'maybe'` → `CheckViolationError`.

**Default-value tests:**
12. `test_concept_problem_tier_defaults_1` — insert omitting `tier` → row reads `tier=1` (server_default). (This is the raw-DDL default; the ORM/seed-helper default is 2 — both are asserted, in different files.)
13. `test_ingest_runs_numeric_and_counter_defaults` — insert minimal run (only `search_space_id`, `document_id`) → `status='queued'`, all counters `0`, `llm_cost_usd=0` (NUMERIC), `created_at` non-null.
14. `test_provenance_defaults_empty_jsonb` — insert omitting `provenance` → `{}`.

**RLS-enable tests (all 5 tables):**
15. `test_rls_enabled_on_five_new_tables` — `pg_class.relrowsecurity` is true for `apollo_ingest_runs`, `apollo_provisioning_jobs`, `apollo_rejected_problems`, `apollo_dedup_decisions`, `apollo_ingest_errors` (parametrize over the 5). Asserts default-deny stopgap is in place with NO policies (`pg_policies` count = 0 for each).

**Backfill tests (use `pre030_factory` — populated pre-030 DB):**
16. `test_backfill_b_flips_seeded_tier1_to_tier2_authored` — MUTATION-PROVE (a): seed (pre-030) a course→subject→concept→concept_problem row (an 018-shape row, no tier col yet), apply 030, assert the row is now `tier=2, solution_source='authored'`. This is the bernoulli-pool-survival guarantee. (The selector-side mutation proof is in File 2.)
17. `test_backfill_a_denormalizes_search_space_id` — seed the same chain with a known `search_space_id`, apply 030, assert `apollo_concept_problems.search_space_id` equals the seeded course id (populated via the concept→subject join), for the row inserted pre-030.
18. `test_backfill_a_noop_when_no_rows` — empty `apollo_concept_problems` pre-030 → 030 applies cleanly, zero rows, columns exist. (Proves the migration is safe on a fresh DB.)

**Partial-unique-index collapse tests (the idempotent-enqueue contract):**
19. `test_open_job_partial_unique_blocks_second_open_job` — insert a `pending` job for `document_id=D`; a second `pending` (or `running`) job for the same `D` → `UniqueViolationError`. (Two OPEN jobs for one doc collapse.)
20. `test_completed_plus_new_open_job_allowed` — insert a `completed` job for `D`, THEN a `pending` job for the same `D` inserts fine (the partial index only covers `state IN ('pending','running')`). Control: a `failed` + a new `pending` for the same `D` also inserts.

**FK-cascade tests (course isolation + observability lifecycle):**
21. `test_ingest_runs_course_cascade` — `DELETE FROM aita_search_spaces WHERE id=<A>` removes course-A's `apollo_ingest_runs` rows (ON DELETE CASCADE).
22. `test_provisioning_jobs_ingest_run_set_null` — delete the referenced `apollo_ingest_runs` row → the job's `ingest_run_id` goes NULL (ON DELETE SET NULL), job survives.
23. `test_child_tables_ingest_run_cascade` — a `rejected_problem`/`dedup_decision`/`ingest_error` row is deleted when its `ingest_run_id` parent is deleted (ON DELETE CASCADE).
24. `test_rejected_dedup_concept_set_null` — deleting the referenced `apollo_concepts` row sets `concept_id` NULL on rejected/dedup rows (ON DELETE SET NULL), rows survive.

**Access-control / isolation (positive + negative + cascade):**
25. `test_ingest_runs_scoped_select_returns_rows` — seed two courses each with one ingest_run; `WHERE search_space_id=<A>` returns exactly course-A's row.
26. `test_ingest_runs_other_tenant_returns_zero_rows` — the same query with course-B's id returns ZERO rows for course-A's data.
27. `test_course_delete_cascades_only_provisioning_rows` — deleting course-A removes only A's provisioning rows; course-B's intact.

### File 2 (EDIT): `apollo/overseer/tests/test_problem_selector.py` — Tier-1 excluded / Tier-2 included

Add tests using the updated `seed_problems(..., tier=...)`:
28. `test_list_problems_excludes_tier1` — seed two problems under one concept, one `tier=1`, one `tier=2`; `list_problems_for_concept` returns ONLY the `tier=2` problem (the `tier=1` id is absent). MUTATION-PROOF: reverting the `tier == 2` WHERE clause makes the Tier-1 row leak → this test REDs.
29. `test_list_problems_includes_tier2` — seed a `tier=2` problem; it is returned (the positive control; proves the filter doesn't over-exclude). Combined with #28, reverting the WHERE either leaks Tier-1 (#28 RED) or, if someone wrote `tier == 1`, drops Tier-2 (#29 RED).
30. `test_select_problem_personalized_also_tier_gated` — with the personalization flag OFF (default), seed one `tier=1` + one `tier=2` intro problem; `select_problem_personalized` returns the Tier-2 problem (proves the single predicate gates the personalized caller too, no separate edit). (Flag-OFF path delegates to `select_problem` → `list_problems_for_concept`.)

The existing tests in this file (`test_list_problems_for_concept_returns_db_problems`, etc.) stay green because `seed_problems`/`seed_course` now default `tier=2`. Confirm by running the whole file.

### File 3 (EDIT): `apollo/persistence/tests/` — ORM parity (place in the existing test dir; e.g. `test_autoprovisioning_models.py` NEW, or extend an existing models test)

These exercise the ORM additions on the `db_session` (`create_all`-built) schema so the ORM edits in `apollo/` are measured by diff-cover (run with `--cov=.`):
31. `test_orm_concept_problem_has_tier_columns` — round-trip insert a `ConceptProblem(..., tier=2, solution_source='authored', provenance={'document_id': 1}, search_space_id=<sid>)`, reload, assert the values persist; insert omitting `tier` → ORM default 2 (the seed-helper/ORM default), and omitting `provenance` → `{}`.
32. `test_orm_kg_entity_scope_summary_roundtrip` — insert a `KGEntity(..., scope_summary='pressure-velocity relation')`, reload, assert it persists; omitting it → NULL.
33. `test_orm_ingest_run_defaults` — insert `IngestRun(search_space_id=<sid>, document_id=1)`, reload, assert `status='queued'`, counters 0, `llm_cost_usd == 0`.
34. `test_orm_provisioning_job_roundtrip` — insert `ProvisioningJob(search_space_id=<sid>, document_id=1)`, reload, assert `state='pending'`, `attempt_count=0`; insert with `lease_owner`/`lease_expires_at`/`ingest_run_id` set, reload, assert they persist.
35. `test_orm_rejected_dedup_error_roundtrip` — insert one `RejectedProblem`, one `DedupDecision`, one `IngestError` (minimal required fields), reload each, assert the typed columns round-trip (incl. `_JSONType` payload/context as dict, `similarity` REAL as float).

(These use `seed_search_space` / a minimal course chain from `_curriculum_fixtures` for the FK targets; `aita_search_spaces` is in `Base.metadata` so `create_all` provides it.)

**Coverage check:** every changed line in `apollo/persistence/models.py` (the 6 column adds + 5 classes), `apollo/overseer/problem_selector.py` (the predicate), and `apollo/subjects/tests/_curriculum_fixtures.py` (the `tier` param) is exercised by Files 2+3. The migration SQL line coverage does not apply (DB-coverage contract) — File 1 enumerates ≥95% of DB behaviors (every column, every CHECK, every RLS-enable, both backfills, the partial-index collapse, the content_hash index, the (concept_id,tier) index).

## Rollback plan

The rollback is the comment block at the foot of `030_apollo_autoprovisioning.sql` (verbatim above). It is run MANUALLY by a human/CI at deploy time if needed — never auto-applied. Exact reverse-order SQL:
```sql
BEGIN;
DROP TABLE IF EXISTS apollo_ingest_errors, apollo_dedup_decisions, apollo_rejected_problems,
  apollo_provisioning_jobs, apollo_ingest_runs;
ALTER TABLE apollo_kg_entities DROP COLUMN IF EXISTS scope_summary;
DROP INDEX IF EXISTS apollo_concept_problems_concept_tier_idx;
ALTER TABLE apollo_concept_problems
  DROP COLUMN IF EXISTS search_space_id, DROP COLUMN IF EXISTS quarantined_at,
  DROP COLUMN IF EXISTS provenance, DROP COLUMN IF EXISTS solution_source, DROP COLUMN IF EXISTS tier;
COMMIT;
```
**Rollback caveat (document in the migration header):** dropping `tier` loses the tier=1/tier=2 distinction, and the backfill (b) that flipped seeded rows to tier=2 is NOT separately reversible — after rollback every problem is teachable again (the pre-030 behavior), which is the safe direction. The `search_space_id` denormalization is dropped cleanly (it was a copy; the authoritative scope is still reachable via the concept→subject join).

The ORM/selector/seed-helper changes are reverted by `git revert` of the commit — there is no DB state tied to them beyond the migration.

## Verification commands (ALL local — executor runs these)

Use the project interpreter explicitly (a bare `python` is an interpreter-selection error, NOT a blocker):
- [ ] **Replay chain + 030 into a fresh local DB & run the migration suite:**
  `.venv/Scripts/python.exe -m pytest tests/database/test_apollo_autoprovisioning_migration.py -v --tb=short` — expect: ALL green, NOT skipped (Docker is up; the pgvector container starts). This proves every column/CHECK/RLS-enable/backfill/partial-index/content_hash-index behavior on real PG.
- [ ] **Selector tier-gate tests:**
  `.venv/Scripts/python.exe -m pytest apollo/overseer/tests/test_problem_selector.py -v` — expect: green incl. the new Tier-1-excluded / Tier-2-included / personalized-also-gated tests, and all pre-existing tests still pass (seed default = tier 2).
- [ ] **ORM parity tests:**
  `.venv/Scripts/python.exe -m pytest apollo/persistence/tests/test_autoprovisioning_models.py -v` — expect: green.
- [ ] **Blast-radius regression (the ~14 seed_problems consumers):**
  `.venv/Scripts/python.exe -m pytest tests/database/test_personalization_select_route_postgres.py apollo/handlers/tests/test_next_db.py apollo/handlers/tests/test_lifecycle_db.py apollo/subjects/tests/test_curriculum_db.py tests/database/test_apollo_curriculum_isolation.py -q` — expect: all green (proves the seed-helper tier-default keeps downstream suites passing). If any RED, the seed-helper default or a missed selector caller is the cause.
- [ ] **MUTATION PROOF (run manually, then revert):** temporarily revert the `tier == 2` WHERE clause → `test_list_problems_excludes_tier1` must RED. Temporarily revert backfill (b)'s `UPDATE ... SET tier=2` line → `test_backfill_b_flips_seeded_tier1_to_tier2_authored` must RED (and, end-to-end, the bernoulli pool would vanish). Re-apply both before committing.
- [ ] **Patch coverage gate:**
  `.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml` then
  `.venv/Scripts/python.exe -m diff_cover coverage.xml --compare-branch=feat/apollo-kg-wu6a3-selection-wiring --fail-under=95` — expect: ≥95% patch coverage on the changed `apollo/` lines.
- [ ] **Lint:** `.venv/Scripts/python.exe -m ruff check apollo/ tests/database/test_apollo_autoprovisioning_migration.py` — expect: no new warnings.

## Deploy handoff (HUMAN/CI only — never executed by feller)

These are for a HUMAN/CI at deploy time. Feller agents do NOT run them.
1. Rehearse 030 on the **TEST** Supabase project (`hjevtxdtrkxjcaaexdxt`) first — verify the applied-migration set, confirm 018+026 are present (backfill (a)'s join + `apollo_kg_entities` FK target depend on them), then apply 030.
2. Run Supabase advisors (`get_advisors`) post-apply to catch RLS/lint regressions on the 5 new tables.
3. Sanity queries against real data on TEST: `SELECT tier, count(*) FROM apollo_concept_problems GROUP BY tier` (expect all existing rows now tier=2); `SELECT count(*) FROM apollo_concept_problems WHERE search_space_id IS NULL` (expect 0 for rows whose concept→subject chain resolves).
4. Only after TEST is clean: a human applies 030 to **PROD** (`uduxdniieeqbljtwocxy`) and re-runs the same sanity queries. PROD applied-state is UNVERIFIED — do not query prod from an agent.

## Downstream consumers

Grep the app source for the affected tables/symbols before/after the change:
```bash
grep -rn "list_problems_for_concept\|ConceptProblem\|apollo_concept_problems" apollo/ knowledge/ retrieval/
grep -rn "seed_problems\|seed_course" apollo/ tests/
```
- **`list_problems_for_concept`** consumers: `select_problem` + `select_problem_personalized` (same file) — both auto-gated by the predicate. No other caller (verified).
- **`seed_problems` / `seed_course`** consumers: ~14 test files (listed in "Test seed-helper change"). The `tier=2` default keeps them green.
- **The 5 new tables + `scope_summary` + `provenance`/etc.**: zero runtime consumers in THIS unit — they are substrate for 3B2b–3B2h. `apollo_provisioning_jobs` is drained by 3B2f; `apollo_ingest_runs`/`rejected`/`dedup`/`errors` written by 3B2g; `scope_summary` embedded by 3B2c; `quarantined_at` filtered by 3B2h.

## Owner-doc updates (drift contract — same commit)

Both owner docs updated in the SAME commit; bump `last_verified: 2026-06-19` in both frontmatters.

**`docs/architecture/domain-data.md`** (owns `database/` migrations + the apollo persistence schema surface):
- Register migration **030_apollo_autoprovisioning.sql** in the migration list/landmarks: the 5 new `apollo_concept_problems` columns, the `apollo_kg_entities.scope_summary` column, the 5 new tables, the two in-migration backfills, the RLS stopgap.
- Register the 5 new ORM classes (`IngestRun`, `ProvisioningJob`, `RejectedProblem`, `DedupDecision`, `IngestError`) + the `ConceptProblem`/`KGEntity` column adds in the ORM-interface section.
- Note the `search_space_id` denormalization on `apollo_concept_problems` (NULLABLE in v1; tighten deferred) and the seed-helper `tier=2` default convention.

**`docs/architecture/apollo.md`** (owns `apollo/`):
- Register that `list_problems_for_concept` is now **Tier-2-gated**, and that this single predicate gates BOTH `select_problem` and `select_problem_personalized` (no separate selector edit).
- Note the `quarantined_at` column exists but its filter is added by WU-3B2h (not this unit).
- (Forward-looking, optional one line: the auto-provisioning substrate now exists; the pipeline lands in 3B2b–3B2h. Do NOT register `APOLLO_AUTOPROVISION_ENABLED` or `apollo/provisioning/**` — those are later units.)

## Risks

- **[HIGH] Backfill (b) is the bernoulli-pool-survival line.** If omitted/wrong, every existing teachable problem becomes tier=1 → invisible the instant the filter ships → Apollo has zero teachable problems in any seeded course. Mitigated by `test_backfill_b_flips_seeded_tier1_to_tier2_authored` (mutation-proved). Confidence: HIGH that the test catches it.
- **[HIGH] Seed-helper default is the test-suite-survival line.** If `seed_problems` is NOT defaulted to tier=2, ~14 downstream real-PG suites break (they seed then select). Mitigated by the explicit seed-helper edit + the blast-radius regression run. Confidence: HIGH.
- **[MEDIUM] Chain selector mis-derivation in the migration harness.** If `_TOUCHES_TARGETS` omits 026, the chain won't create `apollo_kg_entities` (a 030 FK target) or `apollo_subjects.search_space_id` (backfill (a)'s join column) → 030 fails to apply in-test. Mitigated by `test_chain_order_018_before_026_before_030` printing/asserting the resolved chain. Confidence: MEDIUM — the executor MUST verify the resolved list, not assume.
- **[MEDIUM] ORM `create_all` vs migration parity for JSONB `server_default`.** The `provenance` server_default literal `'{}'::jsonb` may not round-trip through SQLite `create_all`. Mitigated by the documented fallback (drop `server_default`, keep `default=dict`) — the real-PG schema comes from the migration, and the `db_session` tests only need the column to exist + default to dict on insert. Confidence: MEDIUM.
- **[LOW] `status` (queued) vs `state` (pending) vocabulary divergence.** `apollo_ingest_runs.status` starts at `'queued'`; `apollo_provisioning_jobs.state` starts at `'pending'`. This is intentional (two different lifecycles) but easy to conflate. Mitigated by tests #8/#9 asserting each rejects the other's vocabulary. Confidence: LOW risk.
- **[LOW] Lock duration on a large `apollo_concept_problems`.** The DEFAULT add is metadata-only on PG11+; the curriculum table is small. Real impact only at deploy (handoff note), not local. Confidence: LOW.

## Deviations I'd allow the executor

- **Test file naming / placement** for File 3 (the ORM parity tests) — e.g. extend an existing `apollo/persistence/tests/test_*.py` instead of a new `test_autoprovisioning_models.py`, as long as the ORM additions are covered and measured by diff-cover.
- **Harness helper reuse vs copy** — importing `_expect_violation` / the `pre*_factory` shape from a shared helper instead of copying, if a clean import path exists; otherwise copy verbatim (the 026 harness copies, so copying is acceptable).
- **Header-comment wording** in the migration (the DDL body is FIXED; the comment prose is the executor's).
- **The `provenance` server_default fallback** (drop the `::jsonb` server_default, keep `default=dict`) if SQLite `create_all` rejects the literal.
- **Extra negative-case tests** beyond the enumerated list (more CHECK rejections, more cascade directions) — welcome, never fewer.

**NOT allowed to deviate:** the pinned DDL body; the `tier == 2` (not `>= 2`, not `!= 1`) predicate; adding `quarantined_at IS NULL` (that is 3B2h); adding any pipeline/LLM logic; adding any new package (BLOCK + escalate); applying 030 to any remote DB; the `search_space_id` NULLABLE-on-concept-problems / NOT-NULL-on-new-tables split (ADJUDICATION #3).
