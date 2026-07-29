# Plan: Apollo KG WU-3A — learner-model migration 026 + ORM (+ apollo_subjects.search_space_id backfill)

**Goal:** Ship migration `026_apollo_learner_model.sql` (the 6 learner-model/grading-core tables + `apollo_kg_entities`/`apollo_entity_prereqs`, `apollo_subjects.search_space_id` with a safe backfill, and `apollo_problem_attempts.learner_update_pending`) plus the matching SQLAlchemy ORM models, fully TDD-covered on a LOCAL Docker Postgres.
**Architecture:** Additive Postgres migration (8 new tables + 2 column adds) on Supabase Postgres 16 + pgvector; raw numbered-SQL migration (NOT Alembic); SQLAlchemy 2.x async ORM mirror with no DB CHECK constraints declared (repo convention).
**Tech stack:** Supabase Postgres 16 + pgvector (`pgvector/pgvector:pg16`), SQLAlchemy async + asyncpg, Testcontainers Postgres harness (`tests/conftest.py::_pg_url`), pytest + pytest-asyncio.

---
provides:
  - migration database/migrations/026_apollo_learner_model.sql
  - table apollo_kg_entities
  - table apollo_entity_prereqs
  - table apollo_learner_state
  - table apollo_mastery_events
  - table apollo_graph_comparison_runs
  - table apollo_graph_comparison_findings
  - column apollo_subjects.search_space_id
  - column apollo_problem_attempts.learner_update_pending
  - ORM models KGEntity, EntityPrereq, LearnerState, MasteryEvent, GraphComparisonRun, GraphComparisonFinding (apollo/persistence/models.py)
  - ORM Subject.search_space_id + ProblemAttempt.learner_update_pending
consumes:
  - apollo_concepts (migration 018)
  - apollo_subjects (migration 018)
  - apollo_problem_attempts (migration 009/025)
  - aita_search_spaces (database/models.py)
  - auth.users (Supabase managed schema)
depends_on:
  - none (WU-3A is the schema base for WU-3B seed, WU-3C resolver, WU-3D cutover)
---

## Overview

WU-3A lays the persistent schema for the Apollo learner model (spec §2/§3) and the
classroom-isolation invariant (spec §1.4). It is **schema + ORM ONLY** — no resolver,
no `:Canon`/`RESOLVES_TO`, no seed script, no §8A runtime cutover.

What changes:

1. **`apollo_subjects.search_space_id`** — the structural link that makes the entire
   curriculum chain course-scoped (concept → subject → search_space). Lands `NOT NULL`
   via a **two-step nullable→backfill→tighten** so it never breaks a populated DB
   (spec §2 "backfill landmine").
2. **Layer 1 — skill inventory:** `apollo_kg_entities` (per-concept `UNIQUE(concept_id,
   canonical_key)`, NOT global — spec §1.4) + `apollo_entity_prereqs` (the normalized
   concept DAG).
3. **Layer 3 — learner model:** `apollo_learner_state` (current snapshot, in-place) +
   `apollo_mastery_events` (append-only log with `UNIQUE NULLS NOT DISTINCT`).
4. **Grading core audit tables:** `apollo_graph_comparison_runs` +
   `apollo_graph_comparison_findings`.
5. **`apollo_problem_attempts.learner_update_pending BOOLEAN`** — the retry flag the
   §6/§7 cross-store pipeline sets when a learner update is deferred.
6. **ORM mirror** in `apollo/persistence/models.py`: six new model classes plus
   `Subject.search_space_id` and `ProblemAttempt.learner_update_pending`. Following the
   repo convention, the ORM declares **no DB CHECK constraints** — the migration SQL is
   the authority; the ORM is the typed Python access layer.

Because this unit's value is DDL behavior (FK cascades, CHECKs, `NULLS NOT DISTINCT`,
the backfill), the real coverage is a **behavioral migration test on a LOCAL Docker
Postgres** that applies the actual migration chain + 026 and exercises every
constraint branch — SQLite cannot honor `NULLS NOT DISTINCT`, array `CHECK`, or FK
cascade, so it is forbidden for the DDL tests (it stays only for the fast
metadata/ORM-shape unit tests, matching repo convention).

## Structural prep (from neighborhood scan)

- [x] **`apollo/persistence/models.py` size:** 283 lines today; adding 6 models (~150
  lines) keeps it well under the 800-line ceiling. **No split needed.** Models stay in
  the one file (the repo's single-module convention for `apollo_*` tables; all existing
  tests import from `apollo.persistence.models`).
- [x] **Table fan-in:** the tables 026 references (`apollo_concepts`, `apollo_subjects`,
  `apollo_problem_attempts`, `aita_search_spaces`, `auth.users`) are all stable hubs
  with additive-only changes here. No existing function/view is modified — 026 is purely
  additive except the `apollo_subjects` column add, which is backward-compatible.
- [x] **Migration-test harness:** the pre-existing content-scoped chain pattern
  (`tests/database/test_teacher_uploads_constraints.py`,
  `tests/database/test_apollo_attempt_result_constraint.py`) is reused verbatim. **No
  new harness infra needed** — `_pg_url` (session pgvector container) already exists.
- **Verify:** `wc -l apollo/persistence/models.py` after edit < 800; `grep -c "class "
  apollo/persistence/models.py` increases by exactly 6.

Neighborhood is clean. Structural prep is 0% of plan steps — no debt to pay down first.

## Prior art (existing migrations + tests)

- `database/migrations/018_apollo_concept_registry.sql:31-110` — the curriculum tables
  this plan extends. Conventions to mirror: `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY
  KEY`, `TIMESTAMPTZ NOT NULL DEFAULT now()`, `JSONB NOT NULL DEFAULT '{}'::jsonb`,
  `UNIQUE (parent_id, slug)`, `CREATE INDEX IF NOT EXISTS <table>_<col>_idx`, `COMMENT ON
  TABLE/COLUMN`, wrapped in `BEGIN; … COMMIT;`.
- `database/migrations/019_apollo_misconceptions.sql:21-51` — per-concept `UNIQUE
  (concept_id, code)` and `REFERENCES apollo_concepts(id) ON DELETE CASCADE`: the exact
  pattern for `apollo_kg_entities`'s per-concept uniqueness and FK cascade.
- `database/migrations/023_apollo_auth_scoping.sql:18-58` — the **backfill pattern**:
  `ADD COLUMN` nullable → `UPDATE … FROM course_memberships`/derived source → `ALTER
  COLUMN … SET NOT NULL` → `ADD CONSTRAINT … FOREIGN KEY … REFERENCES … ON DELETE
  CASCADE`. The `apollo_subjects.search_space_id` backfill follows this 3-phase shape in
  one migration file.
- `database/migrations/025_apollo_attempt_result_values.sql` + its two tests
  (`apollo/persistence/tests/test_attempt_result_values.py` fast unit;
  `tests/database/test_apollo_attempt_result_constraint.py` real-Postgres) — the
  **dual-test convention**: a fast SQLite/metadata test guarding ORM↔migration agreement,
  plus a real-Postgres `asyncpg` test exercising the constraint. WU-3A follows this split.
- `tests/database/test_teacher_uploads_constraints.py:33-82` — the **content-scoped
  migration-chain fixture** with the `auth.users`/`aita_search_spaces` FK stub DDL
  (`_STUB_DDL`), `CREATE DATABASE`, chain application, and per-test rollback
  transaction. 026's migration test reuses this fixture shape exactly.
- `apollo/persistence/tests/test_negotiation_model.py:135-156` — the ORM-shape assertion
  convention: inspect `__table__.columns` / `foreign_keys` / `.ondelete` rather than
  relying on SQLite to enforce Postgres semantics.

## Schema changes (migration 026 — exact DDL)

**File:** `database/migrations/026_apollo_learner_model.sql` (NEW). Next free number is
**026** (on-disk top is 025; 023 is a known prod/test collision, 024/025 unapplied on
test, prod applied-state unverified — see header note below). Entire file wrapped in one
`BEGIN; … COMMIT;`. Copy-pasteable; every statement commented.

### Header comment block (MUST include the reconciliation note)

```sql
-- 026_apollo_learner_model.sql
-- Apollo KG learner model (spec: docs/superpowers/specs/
--   2026-06-10-apollo-kg-learner-model-architecture-decision.md §1.4/§2/§3).
-- WU-3A: schema + ORM only. No resolver (:Canon/RESOLVES_TO — WU-3C), no seed
-- (WU-3B), no §8A runtime cutover (WU-3D).
--
-- Adds: apollo_subjects.search_space_id (course-scoping, isolation invariant §1.4);
--       Layer 1 (apollo_kg_entities, apollo_entity_prereqs);
--       Layer 3 (apollo_learner_state, apollo_mastery_events);
--       grading-core audit (apollo_graph_comparison_runs, _findings);
--       apollo_problem_attempts.learner_update_pending (§6/§7 retry flag).
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 025; 026 is the next free number.
--   * 023 is a TWO-FILE collision (023_apollo_auth_scoping.sql +
--     023_chunks_halfvec_hnsw.sql), resolved on supabase-test by distinct version
--     timestamps. Verify the target project's applied set before applying 026.
--   * 024 (teacher_textbook) + 025 (attempt result values) are NOT applied on
--     supabase-test as of 2026-06-15. PROD applied-state is UNVERIFIED — do not query
--     prod. A human/CI step rehearses on test, then prod (see plan "Deploy handoff").
--   * This file is applied to LOCAL Docker Postgres only by feller agents.
--
-- BACKFILL LANDMINE (spec §2): apollo_subjects.search_space_id lands NOT NULL on a
-- table that may already hold global rows. The migration adds it NULLABLE, backfills
-- every existing subject to a course, THEN tightens to NOT NULL + FK. A bare NOT NULL
-- ADD would fail on any populated environment.

BEGIN;
```

### 1. apollo_subjects.search_space_id (course-scoping, two-step safe backfill)

```sql
-- Phase A: add nullable so the column can be populated before the constraint exists.
ALTER TABLE apollo_subjects
    ADD COLUMN IF NOT EXISTS search_space_id INTEGER;

-- Phase B: backfill existing global subjects to a course.
-- Deterministic rule: attribute each orphan subject to the LOWEST search_space id
-- that exists (the bootstrap/pilot course). This is the seam WU-3B's seeder overrides
-- with the real per-course mapping; here it guarantees NO row is left NULL so Phase C
-- can tighten. If aita_search_spaces is empty (fresh test DB with no course), the
-- UPDATE is a no-op and the (also-empty) apollo_subjects table tightens cleanly.
UPDATE apollo_subjects s
SET search_space_id = (SELECT MIN(id) FROM aita_search_spaces)
WHERE s.search_space_id IS NULL
  AND EXISTS (SELECT 1 FROM aita_search_spaces);

-- Phase C: any subject still NULL has no course to attribute to (a populated
-- apollo_subjects with an empty aita_search_spaces — an inconsistent state). Fail loud
-- rather than silently dropping curriculum: a bare SET NOT NULL below would error, but
-- this explicit guard gives a readable message.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM apollo_subjects WHERE search_space_id IS NULL) THEN
        RAISE EXCEPTION
            'apollo_subjects has rows that cannot be attributed to a course '
            '(no aita_search_spaces rows). Seed a course before applying 026.';
    END IF;
END $$;

-- Phase D: tighten + FK (isolation invariant §1.4 — ON DELETE CASCADE so deleting a
-- course removes its curriculum chain).
ALTER TABLE apollo_subjects
    ALTER COLUMN search_space_id SET NOT NULL,
    ADD CONSTRAINT apollo_subjects_search_space_id_fkey
        FOREIGN KEY (search_space_id)
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS apollo_subjects_search_space_idx
    ON apollo_subjects(search_space_id);

COMMENT ON COLUMN apollo_subjects.search_space_id IS
    'Course owning this subject (isolation invariant §1.4). Concepts/entities/'
    'problems inherit course ownership through existing FKs. Backfilled in 026; '
    'WU-3B seeder sets the real per-course mapping.';
```

### 2. Layer 1 — apollo_kg_entities + apollo_entity_prereqs

```sql
CREATE TABLE IF NOT EXISTS apollo_kg_entities (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id    BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    canonical_key TEXT   NOT NULL,           -- 'bernoulli_principle/eq.bernoulli_full'
    kind          TEXT   NOT NULL
        CHECK (kind IN ('concept','equation','condition','definition','procedure','variable','misconception')),
    display_name  TEXT   NOT NULL,
    payload       JSONB  NOT NULL DEFAULT '{}'::jsonb,  -- symbolic form, applies_when,
                                                        -- opposes_entity_id (misconceptions)
    aliases       JSONB  NOT NULL DEFAULT '[]'::jsonb,  -- grows from the resolution log
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PER-CONCEPT uniqueness, NOT global (§1.4): the same concept name in two courses
    -- is two distinct concepts → two distinct entity rows. Never UNIQUE(canonical_key).
    UNIQUE (concept_id, canonical_key)
);

CREATE INDEX IF NOT EXISTS apollo_kg_entities_concept_idx
    ON apollo_kg_entities(concept_id);

COMMENT ON TABLE apollo_kg_entities IS
    'Layer-1 course-scoped skill inventory (spec §2). Course ownership inherited via '
    'concept_id -> apollo_concepts -> apollo_subjects.search_space_id. canonical_key is '
    'unique PER CONCEPT, not global (§1.4).';

CREATE TABLE IF NOT EXISTS apollo_entity_prereqs (  -- normalizes concept_dag.json
    from_entity_id BIGINT NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    to_entity_id   BIGINT NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    PRIMARY KEY (from_entity_id, to_entity_id)
);

COMMENT ON TABLE apollo_entity_prereqs IS
    'Prerequisite DAG edges between Layer-1 entities (spec §2). from depends on to.';
```

### 3. Layer 3 — apollo_learner_state (current snapshot)

```sql
CREATE TABLE IF NOT EXISTS apollo_learner_state (
    user_id            UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    entity_id          BIGINT  NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    belief             REAL[]  NOT NULL CHECK (array_length(belief, 1) = 3),
                                          -- [p_misc, p_shaky, p_mastered]; sums-to-1 app-side
    mastery            REAL    NOT NULL CHECK (mastery >= 0 AND mastery <= 1),
    confidence         REAL    NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    misconception_code TEXT    NULL,
    evidence_count     INTEGER NOT NULL DEFAULT 0,
    last_evidence_at   TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, search_space_id, entity_id)
);

COMMENT ON TABLE apollo_learner_state IS
    'Layer-3 current per-(user,course,entity) belief snapshot, updated in place at Done '
    '(spec §3). PK enforces one row per learner per entity per course.';
```

> **Note on `entity_id` FK cascade:** the spec §2 sketch leaves `apollo_learner_state`/
> `apollo_mastery_events` `entity_id` FKs without an explicit `ON DELETE`. WU-3A makes
> them **`ON DELETE CASCADE`** — deleting a course cascades search_space → subject →
> concept → entity, and learner rows for a deleted entity are meaningless. This is a
> binding decision for this unit; `apollo_mastery_events.attempt_id` stays `ON DELETE
> SET NULL` (the event log outlives a deleted attempt — that is the whole point of the
> `NULLS NOT DISTINCT` dedup, spec §2).

### 4. Layer 3 — apollo_mastery_events (append-only log)

```sql
CREATE TABLE IF NOT EXISTS apollo_mastery_events (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    entity_id          BIGINT  NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    attempt_id         BIGINT  REFERENCES apollo_problem_attempts(id) ON DELETE SET NULL,
    event_kind         TEXT    NOT NULL,   -- covered|missing|partial|misconception|corrected
                                           -- open enum; 'chat_question' reserved (RQ5)
    score              REAL    NULL CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    misconception_code TEXT    NULL,
    parser_confidence  REAL    NULL CHECK (parser_confidence IS NULL OR (parser_confidence >= 0 AND parser_confidence <= 1)),
    grader_confidence  REAL    NULL CHECK (grader_confidence IS NULL OR (grader_confidence >= 0 AND grader_confidence <= 1)),
    negotiation_move   TEXT    NULL,       -- challenge|paraphrase|skip|null
    reference_step_id  TEXT    NULL,
    prior_belief       REAL[]  NOT NULL CHECK (array_length(prior_belief, 1) = 3),
    posterior_belief   REAL[]  NOT NULL CHECK (array_length(posterior_belief, 1) = 3),
    mastery_after      REAL    NOT NULL CHECK (mastery_after >= 0 AND mastery_after <= 1),
    dt_days_since_last REAL    NULL,
    evidence_node_ids  JSONB   NOT NULL DEFAULT '[]'::jsonb,  -- Neo4j bridge
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT: a plain UNIQUE never conflicts on NULL attempt_id (deleted
    -- attempts; reserved chat_question events), so retries could double-insert. This
    -- treats NULL attempt_id as equal, blocking the duplicate. Belt-and-braces — the
    -- real guarantee is transactional (§3). Requires Postgres 15+ (pg16 here).
    CONSTRAINT apollo_mastery_events_attempt_entity_kind_uniq
        UNIQUE NULLS NOT DISTINCT (attempt_id, entity_id, event_kind)
);

CREATE INDEX IF NOT EXISTS apollo_mastery_events_user_entity_created_idx
    ON apollo_mastery_events (user_id, entity_id, created_at);  -- Q3 trend
CREATE INDEX IF NOT EXISTS apollo_mastery_events_entity_created_idx
    ON apollo_mastery_events (entity_id, created_at);           -- Q2 class aggregates

COMMENT ON TABLE apollo_mastery_events IS
    'Layer-3 append-only longitudinal evidence log AND refit corpus (spec §2/§3). '
    'mastery_after is the direct Q3 time series.';
```

### 5. Grading-core audit — apollo_graph_comparison_runs + _findings

```sql
CREATE TABLE IF NOT EXISTS apollo_graph_comparison_runs (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id               BIGINT  NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    user_id                  UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id          INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    coverage_score           REAL NOT NULL,
    soundness_score          REAL NOT NULL,
    bisimilarity_score       REAL NOT NULL,
    node_coverage_score      REAL,
    edge_coverage_score      REAL,
    scoping_score            REAL,
    usage_score              REAL,
    procedure_order_score    REAL,
    dependency_score         REAL,
    contradiction_score      REAL,
    normalization_confidence REAL NOT NULL,
    abstained                BOOLEAN NOT NULL DEFAULT false,
    abstention_reasons       JSONB   NOT NULL DEFAULT '[]'::jsonb,
    comparison_version       TEXT NOT NULL,   -- algorithm version, for replays
    reference_graph_hash     TEXT NOT NULL,   -- the reference graph AS GRADED
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Re-run at the same version SUPERSEDES (delete prior run+findings+events, re-insert
    -- in one txn, §2). Constraint guards accidental double-execution; must not crash a
    -- legitimate retry (retry deletes first).
    CONSTRAINT apollo_graph_comparison_runs_attempt_version_uniq
        UNIQUE (attempt_id, comparison_version)
);

CREATE INDEX IF NOT EXISTS apollo_graph_comparison_runs_attempt_idx
    ON apollo_graph_comparison_runs(attempt_id);

COMMENT ON TABLE apollo_graph_comparison_runs IS
    'One row per Done-time comparison; makes the grader auditable (spec §2/§6).';

CREATE TABLE IF NOT EXISTS apollo_graph_comparison_findings (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES apollo_graph_comparison_runs(id) ON DELETE CASCADE,
    entity_id          BIGINT REFERENCES apollo_kg_entities(id) ON DELETE SET NULL,
    finding_kind       TEXT NOT NULL,  -- covered_node|missing_node|matched_edge|missing_edge
                                       -- |unsupported_extra|contradiction|unresolved|alternative_path
    score              REAL,
    confidence         REAL,
    student_node_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
    reference_node_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    student_edge_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
    reference_edge_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_spans     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- quoted transcript spans
    message            TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS apollo_graph_comparison_findings_run_idx
    ON apollo_graph_comparison_findings(run_id);

COMMENT ON TABLE apollo_graph_comparison_findings IS
    'Structured evidence behind every comparison score; diagnostics read THIS, not the '
    'transcript (spec §2/§6). entity_id ON DELETE SET NULL: a pruned entity must not '
    'delete the audit finding.';
```

> **`apollo_graph_comparison_findings.entity_id` is `ON DELETE SET NULL`** (not CASCADE):
> the finding is an audit record; losing the entity FK must degrade to a null link, never
> erase the audit row. This is a deliberate asymmetry vs the learner-state tables.

### 6. apollo_problem_attempts.learner_update_pending

```sql
-- §6/§7 cross-store retry flag: set true when the learner update is deferred (resolution
-- infra failure) so a janitor/next-session retry re-runs from resolution idempotently.
ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_pending BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN apollo_problem_attempts.learner_update_pending IS
    'True iff a Done-time learner-model update is pending retry (spec §5/§6/§7). The '
    'grade is never voided by grading-pipeline failure; this flag covers the cross-store '
    'window.';

COMMIT;
```

## Access control (isolation invariant + RLS stopgap)

**Engine: Postgres (Supabase), which has row-level security.** The repo's established
mechanism (migration `022_enable_rls_stopgap.sql`) is: **`ENABLE ROW LEVEL SECURITY` on
every public `apollo_*` table, with NO permissive policies**, because the backend
connects as the table **owner** (`postgres`), and owners are exempt from RLS unless
`FORCE ROW LEVEL SECURITY` is set. Net effect: the anon/public PostgREST key cannot
read/write these tables; the FastAPI backend (owner connection) is unaffected and
performs all tenant scoping in the application layer via `auth.py` + `search_space_id`
filters. The UIs never touch `apollo_*` via PostgREST (auth only). This is the exact
same posture every existing `apollo_*` table already has (migration 022).

**For WU-3A, the access-control requirement is met as follows:**

1. **Enable RLS (default-deny to PostgREST) on the 6 new public tables**, mirroring
   migration 022. Append to the END of migration 026 (inside the same transaction is
   fine; `ENABLE ROW LEVEL SECURITY` is transactional):

   ```sql
   ALTER TABLE apollo_kg_entities             ENABLE ROW LEVEL SECURITY;
   ALTER TABLE apollo_entity_prereqs          ENABLE ROW LEVEL SECURITY;
   ALTER TABLE apollo_learner_state           ENABLE ROW LEVEL SECURITY;
   ALTER TABLE apollo_mastery_events          ENABLE ROW LEVEL SECURITY;
   ALTER TABLE apollo_graph_comparison_runs   ENABLE ROW LEVEL SECURITY;
   ALTER TABLE apollo_graph_comparison_findings ENABLE ROW LEVEL SECURITY;
   ```

   No policies are added (matching 022): the tables hold tenant data, RLS-on with no
   policy denies all non-owner access, and the owner-connection backend is the only
   reader/writer. This is the repo's documented default-deny baseline, not an oversight.

2. **Tenant/course isolation enforcement point (the named app-layer point):** every
   learner-model row carries `search_space_id` (directly, or transitively via
   `entity_id → apollo_kg_entities → apollo_concepts → apollo_subjects.search_space_id`).
   The invariant (spec §1.4) is enforced in the application by `auth.require_course_member`
   / `require_session_owner` (already gating every `/apollo/*` route, see
   `docs/architecture/apollo.md`) **plus** the `search_space_id` predicate on every
   learner-model query written in later units (WU-3B+). WU-3A's contribution is the
   schema that makes that predicate possible: the FKs and the `search_space_id` columns.
   The structural guarantee that a row CANNOT belong to two courses is the
   `PRIMARY KEY (user_id, search_space_id, entity_id)` on `apollo_learner_state` and the
   `search_space_id NOT NULL` columns on every learner-model table.

3. **Test queries (run on the LOCAL migrated Postgres, see TDD plan):**
   - **Positive:** insert a learner_state row for `(user_A, space_1, entity_1)` and a
     mastery_event for the same tenant; a `SELECT … WHERE search_space_id = 1 AND user_id
     = user_A` returns exactly those rows. (`test_learner_state_scoped_select_returns_rows`)
   - **Negative:** with rows for `space_1`/`user_A` present, a `SELECT … WHERE
     search_space_id = 2` (a different course) and a `SELECT … WHERE user_id = user_B`
     (a different student) each return **zero rows** — the isolation invariant holds at
     the data layer. (`test_learner_state_other_tenant_returns_zero_rows`)
   - **Cascade isolation:** deleting `aita_search_spaces` row `space_1` cascades and
     removes that course's subject → concept → entity → learner_state/mastery_events,
     leaving `space_2`'s rows untouched. (`test_course_delete_cascades_only_its_curriculum`)

## ORM models (apollo/persistence/models.py)

All new models go in the existing `apollo/persistence/models.py` (still < 800 lines
after). Conventions copied from the current file: `BigInteger().with_variant(Integer(),
"sqlite")` for IDENTITY PKs, `_JSONType` (`JSONB().with_variant(JSON(), "sqlite")`) for
JSONB, `UUID(as_uuid=False)` for user ids, `TIMESTAMP(timezone=True)` with
`default=lambda: datetime.now(UTC)`, **no DB CHECK constraints declared** (the migration
SQL is the authority — same as `ATTEMPT_RESULTS`/`apollo_problem_attempts.result`). For
`REAL[]` belief arrays, use `from sqlalchemy.dialects.postgresql import ARRAY` with
`ARRAY(REAL)` and a SQLite variant of `_JSONType` (SQLite has no array type — store as
JSON in the SQLite variant so the fast metadata tests can still introspect the column).

> **Array-column variant decision (binding):** declare
> `belief = Column(ARRAY(Float).with_variant(_JSONType, "sqlite"), nullable=False)`.
> The Postgres path is a real `REAL[]`; the SQLite path degrades to JSON. The behavioral
> array-CHECK tests run ONLY on real Postgres (per constraints), so the SQLite variant
> is exercised only by shape/metadata tests, never for array semantics.

### New module-level allowlist constants (app-layer mirrors of the SQL CHECKs)

Following the `ATTEMPT_RESULTS` precedent, declare tuples the runtime reasons about,
each asserted equal to the migration's `CHECK`/enum set by a fast unit test:

```python
ENTITY_KINDS = (
    "concept", "equation", "condition", "definition",
    "procedure", "variable", "misconception",
)
# event_kind is an OPEN enum per spec §2 (no SQL CHECK) — documented set, not enforced:
MASTERY_EVENT_KINDS = ("covered", "missing", "partial", "misconception", "corrected")
FINDING_KINDS = (
    "covered_node", "missing_node", "matched_edge", "missing_edge",
    "unsupported_extra", "contradiction", "unresolved", "alternative_path",
)
```

`ENTITY_KINDS` is the one tied to a SQL `CHECK` (on `apollo_kg_entities.kind`) and gets
a migration↔model equality test (mirrors `test_attempt_result_values.py`).
`MASTERY_EVENT_KINDS`/`FINDING_KINDS` are documentation tuples (open enums; no SQL CHECK)
and are NOT equality-asserted against the SQL — asserting an open enum would be wrong.

### Model classes (public signatures)

| Class | `__tablename__` | Key columns (types) | FKs / constraints declared in ORM |
|---|---|---|---|
| `KGEntity` | `apollo_kg_entities` | `id` PK, `concept_id` BigInteger, `canonical_key` Text, `kind` Text, `display_name` Text, `payload` `_JSONType` default `dict`, `aliases` `_JSONType` default `list`, `created_at`/`updated_at` | FK `concept_id → apollo_concepts.id` ondelete CASCADE; `UniqueConstraint(concept_id, canonical_key)`; Index on `concept_id` |
| `EntityPrereq` | `apollo_entity_prereqs` | `from_entity_id` BigInteger PK-part, `to_entity_id` BigInteger PK-part | both FK `→ apollo_kg_entities.id` ondelete CASCADE; composite PK |
| `LearnerState` | `apollo_learner_state` | `user_id` UUID PK-part, `search_space_id` Integer PK-part, `entity_id` BigInteger PK-part, `belief` ARRAY(REAL)/JSON, `mastery` Float, `confidence` Float, `misconception_code` Text null, `evidence_count` Integer default 0, `last_evidence_at` ts null, `updated_at` ts | FKs `user_id → auth.users.id` CASCADE, `search_space_id → aita_search_spaces.id` CASCADE, `entity_id → apollo_kg_entities.id` CASCADE; composite PK `(user_id, search_space_id, entity_id)` |
| `MasteryEvent` | `apollo_mastery_events` | `id` PK, `user_id` UUID, `search_space_id` Integer, `entity_id` BigInteger, `attempt_id` BigInteger null, `event_kind` Text, `score` Float null, `misconception_code` Text null, `parser_confidence`/`grader_confidence` Float null, `negotiation_move` Text null, `reference_step_id` Text null, `prior_belief`/`posterior_belief` ARRAY(REAL)/JSON, `mastery_after` Float, `dt_days_since_last` Float null, `evidence_node_ids` `_JSONType` default list, `created_at` | FKs `user_id`/`search_space_id` CASCADE, `entity_id` CASCADE, `attempt_id → apollo_problem_attempts.id` **SET NULL**; `UniqueConstraint(attempt_id, entity_id, event_kind, postgresql_nulls_not_distinct=True, name="apollo_mastery_events_attempt_entity_kind_uniq")`; two indexes |
| `GraphComparisonRun` | `apollo_graph_comparison_runs` | `id` PK, `attempt_id` BigInteger, `user_id` UUID, `search_space_id` Integer, `coverage_score`/`soundness_score`/`bisimilarity_score`/`normalization_confidence` Float, the 9 nullable sub-scores Float null, `abstained` Boolean default False, `abstention_reasons` `_JSONType` default list, `comparison_version` Text, `reference_graph_hash` Text, `created_at` | FKs `attempt_id` CASCADE, `user_id`/`search_space_id` CASCADE; `UniqueConstraint(attempt_id, comparison_version)`; Index on `attempt_id` |
| `GraphComparisonFinding` | `apollo_graph_comparison_findings` | `id` PK, `run_id` BigInteger, `entity_id` BigInteger null, `finding_kind` Text, `score`/`confidence` Float null, four `*_ids` `_JSONType` default list, `evidence_spans` `_JSONType` default list, `message` Text null, `created_at` | FK `run_id → apollo_graph_comparison_runs.id` CASCADE, `entity_id → apollo_kg_entities.id` **SET NULL**; Index on `run_id` |

**Edits to existing models (backward-compatible):**

- `Subject` — add `search_space_id = Column(Integer, ForeignKey("aita_search_spaces.id",
  ondelete="CASCADE"), nullable=False, index=True)`. **Backward-compat note:** existing
  tests construct `Subject(slug=…, display_name=…)` — search the suite; the fast unit
  tests and any seeder fixtures that build a `Subject` must now pass `search_space_id`.
  This is a required, intentional break (the column is NOT NULL by design); enumerate
  the call sites in the test plan. No production runtime constructs `Subject` today
  (curriculum is filesystem-read in v1), so the blast radius is test fixtures only.
- `ProblemAttempt` — add `learner_update_pending = Column(Boolean, nullable=False,
  server_default=text("false"), default=False)`. `server_default` so the column is
  present for rows inserted by raw SQL/older code paths; `default=False` for ORM inserts.
  Backward-compatible: every existing `ProblemAttempt(...)` construction still works
  (defaults to False).
- Add `Boolean`, `Float`, `text`, `UniqueConstraint`, `PrimaryKeyConstraint` (as needed)
  and `ARRAY` to the imports.

All models are **append-only additions** — no existing column is renamed or dropped, so
existing imports and tests keep working except the `Subject(search_space_id=…)` fixture
break called out above.

## Migration plan

Single, non-destructive, additive migration (the `apollo_subjects.search_space_id` add
is the only column-tighten, and it is made safe by the in-file two-step backfill — so
**no 2-phase / separate-deploy split is required**). One file, one transaction.

- [x] **Phase 1 migration file:** `database/migrations/026_apollo_learner_model.sql`
  - Contents: the full DDL in "Schema changes" above (header note → `apollo_subjects`
    backfill → Layer 1 → Layer 3 → grading-core tables → `learner_update_pending` →
    RLS-enable block), all inside one `BEGIN; … COMMIT;`.
  - **Why not 2-phase:** the only potentially-destructive operation is `SET NOT NULL` on
    a freshly-added column; the backfill (Phase B) + the explicit guard (Phase C) make
    it safe on a populated DB without a separate deploy. Everything else is `CREATE TABLE
    IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` (idempotent).
  - Verify (LOCAL only): apply the on-disk migration chain (009 → 018 → 019 → 023 →
    025) plus 026 onto a fresh DB on the session pgvector container, with the
    `auth.users` + `aita_search_spaces` + `course_memberships` FK stubs (see TDD plan
    `_STUB_DDL`). Expect: all 8 tables exist; `apollo_subjects.search_space_id` is `NOT
    NULL` with the FK; `learner_update_pending` defaults false.
  - psql/asyncpg check: `SELECT to_regclass('public.apollo_kg_entities')` etc. → non-null
    for all 8 tables; `SELECT is_nullable FROM information_schema.columns WHERE
    table_name='apollo_subjects' AND column_name='search_space_id'` → `NO`.
- [x] **Phase 2:** **None.** No deferred/destructive second migration in WU-3A. (The
  §8A `concept_cluster_id` drop is WU-3D, explicitly out of scope.)

**No remote apply.** Per CLAUDE.md DB contract + the user's memory
(`feedback_feller_db_local_only`), feller agents apply this only to LOCAL Docker
Postgres. Remote rollout is the human/CI "Deploy handoff" step below.

## Rollback plan

Append this exact rollback block as a trailing comment in
`026_apollo_learner_model.sql` (the repo ships rollback as an inline comment block; see
the migration-safety convention). Drop in reverse-dependency order; the column drops are
last.

```sql
-- Rollback (run manually; never auto-applied):
-- BEGIN;
-- DROP TABLE IF EXISTS apollo_graph_comparison_findings;
-- DROP TABLE IF EXISTS apollo_graph_comparison_runs;
-- DROP TABLE IF EXISTS apollo_mastery_events;
-- DROP TABLE IF EXISTS apollo_learner_state;
-- DROP TABLE IF EXISTS apollo_entity_prereqs;
-- DROP TABLE IF EXISTS apollo_kg_entities;
-- ALTER TABLE apollo_problem_attempts DROP COLUMN IF EXISTS learner_update_pending;
-- ALTER TABLE apollo_subjects
--     DROP CONSTRAINT IF EXISTS apollo_subjects_search_space_id_fkey;
-- DROP INDEX IF EXISTS apollo_subjects_search_space_idx;
-- ALTER TABLE apollo_subjects DROP COLUMN IF EXISTS search_space_id;
-- COMMIT;
```

The rollback is safe because 026 is purely additive: dropping the new tables/columns
returns `apollo_subjects` and `apollo_problem_attempts` to their pre-026 shape with no
data loss for any pre-existing column (only the new `search_space_id`/
`learner_update_pending` values, which no pre-026 code reads, are lost).

## TDD test plan (REAL Postgres + fast unit tests)

**TDD order: write the test files first (RED), then the migration SQL + ORM (GREEN).**
No skip-marks, no xfail, no assert-nothing. The LLM is never called in this unit — these
are pure DB/ORM tests; the only mocking is the `auth.users`/`aita_search_spaces`/
`course_memberships` FK **stubs** (DDL, not LLM). The session already stubs OpenAI in
`tests/conftest.py`, so no live API can be reached even transitively.

**Harness reuse (no new infra):** the real-Postgres tests reuse the existing
`tests/conftest.py::_pg_url` session fixture (Testcontainers `pgvector/pgvector:pg16`)
and the **content-scoped migration-chain fixture** copied from
`tests/database/test_apollo_attempt_result_constraint.py` /
`test_teacher_uploads_constraints.py`: `CREATE DATABASE` → apply `_STUB_DDL` → apply the
ordered migration chain that touches the target tables → 026 → per-test rollback
transaction via `asyncpg`. **If Docker is unavailable, `_pg_url` skips cleanly** — but
because WU-3A's coverage REQUIRES real Postgres (NULLS NOT DISTINCT / array CHECK / FK
cascade), the executor MUST confirm Docker is up; a skipped DB suite is NOT a passing
unit (see "Verification commands" — the run must show these tests as passed, not
skipped). If Docker cannot run in the executor's environment, that is a **blocker**, not
a SQLite fallback.

### `_STUB_DDL` + migration chain (in the real-Postgres test file)

```sql
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE course_memberships (user_id UUID, search_space_id INTEGER);
```

Chain selection is **content-scoped** (same regex approach as prior art): include every
on-disk migration that creates/alters any of `apollo_sessions`, `apollo_concepts`,
`apollo_subjects`, `apollo_misconceptions`, `apollo_problem_attempts`, in filename order,
then 026. Concretely that is 009, 018, 019, 023, 025, 026. (023's `course_memberships`
backfill UPDATE runs against the empty stub — a no-op; its `auth.users`/
`aita_search_spaces` FKs resolve against the stubs.) A `_seed_course_and_concept()`
helper inserts one `aita_search_spaces` row, one `auth.users` row, one `apollo_subjects`
(now requires `search_space_id`), one `apollo_concepts`, and one `apollo_kg_entities` to
hang learner rows off.

### File 1 — `tests/database/test_apollo_learner_model_migration.py` (REAL Postgres, `@pytest.mark.integration`)

Enumerates EVERY constraint/FK/CHECK/UNIQUE/backfill branch of migration 026:

| Test name | Asserts | Mocking |
|---|---|---|
| `test_all_eight_tables_created` | `to_regclass` non-null for all 8 new tables after the chain+026 | FK stubs only |
| `test_subjects_search_space_id_not_null_and_fk` | `information_schema` shows `search_space_id` `is_nullable='NO'`; FK to `aita_search_spaces` exists | FK stubs |
| `test_subjects_backfill_populates_existing_global_rows` | **Backfill branch:** build a pre-026 DB (chain WITHOUT 026), insert an `aita_search_spaces` row + an `apollo_subjects` row with NO `search_space_id` (pre-026 shape), THEN apply 026 → the existing subject row now has `search_space_id = MIN(spaces.id)` and the column is NOT NULL. Proves the migration does not break a populated DB. | FK stubs; separate DB built without 026 first |
| `test_subjects_backfill_guard_raises_when_no_course` | **Phase C guard:** pre-026 DB with an `apollo_subjects` row but ZERO `aita_search_spaces` rows → applying 026 raises (the `RAISE EXCEPTION` fires) rather than silently dropping | FK stubs |
| `test_subjects_backfill_noop_on_empty` | Empty `apollo_subjects` + empty/clean course table → 026 applies cleanly, column NOT NULL, no rows | FK stubs |
| `test_kg_entities_unique_is_per_concept_not_global` | **Per-concept UNIQUE (§1.4):** same `canonical_key` under TWO different `concept_id`s inserts fine (2 rows); same `(concept_id, canonical_key)` twice raises `UniqueViolationError` | FK stubs + seeded concepts |
| `test_kg_entities_kind_check_rejects_bad_accepts_good` | each value in `ENTITY_KINDS` inserts; a bad kind (`'banana'`) raises `CheckViolationError` | FK stubs |
| `test_kg_entities_fk_concept_cascade` | deleting the parent `apollo_concepts` row deletes its `apollo_kg_entities` rows | FK stubs |
| `test_entity_prereqs_composite_pk_and_cascade` | inserting the same `(from,to)` twice raises; deleting an entity cascades to its prereq edges (both directions) | FK stubs |
| `test_learner_state_pk_blocks_duplicate_tuple` | second insert of the same `(user_id, search_space_id, entity_id)` raises `UniqueViolationError` | FK stubs |
| `test_learner_state_belief_array_check` | `belief` of length 3 inserts; length 2 and length 4 each raise `CheckViolationError` (real PG array_length CHECK) | FK stubs |
| `test_learner_state_mastery_confidence_range_checks` | `mastery`/`confidence` in [0,1] insert; values `-0.1` and `1.1` each raise `CheckViolationError` | FK stubs |
| `test_learner_state_fk_cascades` | deleting the `auth.users` row, the `aita_search_spaces` row, and the `apollo_kg_entities` row each independently cascades the learner_state row away | FK stubs |
| `test_mastery_events_belief_checks` | `prior_belief`/`posterior_belief` length-3 ok; wrong length raises | FK stubs |
| `test_mastery_events_score_confidence_range_checks` | `score`/`parser_confidence`/`grader_confidence`/`mastery_after` accept null + [0,1], reject out-of-range | FK stubs |
| `test_mastery_events_nulls_not_distinct_dedup` | **NULLS NOT DISTINCT (§2):** two rows with the SAME `entity_id`+`event_kind` and `attempt_id = NULL` → the second raises `UniqueViolationError` (a plain UNIQUE would NOT — this is the load-bearing PG15+ behavior); a control with two DIFFERENT non-null `attempt_id`s inserts both | FK stubs |
| `test_mastery_events_attempt_id_set_null_on_delete` | insert event with a real `attempt_id`; delete the `apollo_problem_attempts` row → the event row survives with `attempt_id IS NULL` (NOT cascaded — the log outlives the attempt) | FK stubs |
| `test_mastery_events_entity_cascade` | deleting the entity cascades the mastery_event away | FK stubs |
| `test_comparison_runs_unique_attempt_version` | second run with the same `(attempt_id, comparison_version)` raises; a different `comparison_version` inserts | FK stubs |
| `test_comparison_runs_attempt_cascade` | deleting the attempt cascades the run (and its findings) away | FK stubs |
| `test_comparison_findings_entity_set_null_on_delete` | finding with `entity_id` set; delete the entity → finding survives with `entity_id IS NULL` (audit record preserved) | FK stubs |
| `test_comparison_findings_run_cascade` | deleting the run cascades its findings | FK stubs |
| `test_learner_update_pending_defaults_false` | insert an attempt via raw SQL omitting the column → `learner_update_pending` is `false` (server_default) | FK stubs |
| `test_learner_state_scoped_select_returns_rows` | **Access-control positive:** rows for `(user_A, space_1)` are returned by a `search_space_id=1 AND user_id=user_A` SELECT | FK stubs, 2 seeded courses |
| `test_learner_state_other_tenant_returns_zero_rows` | **Access-control negative:** with only `space_1`/`user_A` rows present, `search_space_id=2` AND `user_id=user_B` each return ZERO rows | FK stubs, 2 seeded courses |
| `test_course_delete_cascades_only_its_curriculum` | deleting `aita_search_spaces` `space_1` removes its subject→concept→entity→learner rows but leaves `space_2`'s chain intact | FK stubs, 2 seeded courses |

### File 2 — `apollo/persistence/tests/test_learner_model_models.py` (fast; in-memory SQLite, metadata only)

Mirrors the `test_negotiation_model.py` / `test_models_auth_scoping.py` convention —
ORM-shape + insert/read-back where SQLite can honor it; Postgres-only semantics asserted
by model inspection, not runtime:

| Test name | Asserts | Mocking |
|---|---|---|
| `test_six_models_map_expected_tablenames` | each new class `.__tablename__` matches the migration table name | none |
| `test_kg_entity_unique_constraint_is_per_concept` | `KGEntity.__table__` has a `UniqueConstraint` over exactly `(concept_id, canonical_key)` — guards against a global-unique regression | none |
| `test_kg_entity_concept_fk_cascade` | `concept_id` FK targets `apollo_concepts`, `ondelete='CASCADE'` | none |
| `test_learner_state_composite_pk` | PK columns are exactly `(user_id, search_space_id, entity_id)` | none |
| `test_learner_state_fk_targets_and_cascades` | the three FKs target `auth.users`/`aita_search_spaces`/`apollo_kg_entities`, all `CASCADE` | none |
| `test_mastery_event_attempt_fk_set_null` | `attempt_id` FK `ondelete='SET NULL'`; entity FK `CASCADE` | none |
| `test_mastery_event_unique_nulls_not_distinct_flag` | the `UniqueConstraint` over `(attempt_id, entity_id, event_kind)` carries `constraint.dialect_options['postgresql']['nulls_not_distinct'] is True` (SQLAlchemy 2.0.x supports `postgresql_nulls_not_distinct` — verified in `dialects/postgresql/base.py:2597`) — guards the load-bearing flag from being dropped | none |
| `test_comparison_run_unique_attempt_version` | `UniqueConstraint(attempt_id, comparison_version)` present | none |
| `test_comparison_finding_entity_set_null` | `entity_id` FK `ondelete='SET NULL'`, `run_id` FK CASCADE | none |
| `test_insert_read_back_learner_state_sqlite` | construct + commit a `LearnerState` (belief stored via JSON variant) and read it back equal | SQLite in-memory (per `test_negotiation_model.py` pattern) |
| `test_insert_read_back_mastery_event_sqlite` | construct + commit a `MasteryEvent`, read back; belief arrays round-trip | SQLite in-memory |
| `test_subject_requires_search_space_id` | `Subject.__table__.columns['search_space_id']` is non-nullable + FK to `aita_search_spaces`, `ondelete='CASCADE'` | none |
| `test_problem_attempt_has_learner_update_pending_default_false` | `ProblemAttempt.__table__.columns['learner_update_pending']` is `Boolean`, non-nullable, server_default present; an ORM-constructed attempt has `.learner_update_pending == False` after flush | SQLite in-memory |

### File 3 — `apollo/persistence/tests/test_learner_model_allowlists.py` (fast; file-read, no DB)

Mirrors `test_attempt_result_values.py` (migration↔model agreement, no DB needed):

| Test name | Asserts | Mocking |
|---|---|---|
| `test_entity_kinds_match_migration_check` | `ENTITY_KINDS` equals the value set inside `apollo_kg_entities … kind IN (...)` parsed from `026_apollo_learner_model.sql` | none (reads the migration text) |
| `test_migration_026_file_exists` | the migration file exists at the expected path | none |
| `test_migration_creates_all_eight_tables` | the file text contains `CREATE TABLE IF NOT EXISTS <t>` for each of the 8 tables | none |
| `test_migration_uses_nulls_not_distinct` | the file contains `UNIQUE NULLS NOT DISTINCT (attempt_id, entity_id, event_kind)` (the spec-mandated dedup) | none |
| `test_migration_kg_entities_unique_is_per_concept` | the file contains `UNIQUE (concept_id, canonical_key)` and does NOT contain a bare `UNIQUE (canonical_key)` | none |
| `test_migration_subjects_backfill_present` | the file backfills `search_space_id` (an `UPDATE apollo_subjects … SET search_space_id`) BEFORE the `SET NOT NULL` — guards against a bare-NOT-NULL regression | none |
| `test_migration_header_notes_reconciliation` | the header comment mentions the 023 collision, 024/025 unapplied-on-test, and "LOCAL" / do-not-apply-remote | none |
| `test_migration_enables_rls_on_new_tables` | the file contains `ENABLE ROW LEVEL SECURITY` for each of the 6 public tables | none |
| `test_mastery_event_and_finding_kinds_documented` | `MASTERY_EVENT_KINDS` / `FINDING_KINDS` are non-empty tuples matching the spec §2/§6.3 sets (documentation guard; not asserted against SQL since they are open enums) | none |

### Coverage accounting (≥95% patch, diff-cover vs `feat/apollo-kg-wu2b-crossturn-writeedges`)

- **Python diff lines** = the ~6 model classes + 3 constant tuples + 2 column edits +
  imports in `apollo/persistence/models.py`. Every model class is constructed/inspected
  by File 2; every constant is asserted by File 3. No Python branch is left uncovered —
  the models are declarative (no `if`/loops), so line coverage tracks 1:1 with "is the
  attribute referenced by a test." Target: 100% of changed `models.py` lines.
- **SQL behavior** (not line-counted per CLAUDE.md DB contract) — File 1 enumerates
  every FK (`ON DELETE CASCADE` ×N, `SET NULL` ×2), every CHECK (kind, 4 array-length,
  6 range), both UNIQUEs (incl. NULLS NOT DISTINCT), the composite PKs, and all three
  backfill branches (populated / empty / guard-raise). That is ≥95% of the
  policy×constraint behaviors of 026 (the only untested behavior is `now()` defaults and
  index existence, which are covered indirectly by inserts/`to_regclass`).
- **Test-file lines** are themselves changed lines; they are covered by executing them.
- `Subject(search_space_id=…)` fixture break — **exactly two call sites** (verified by
  grep `Subject\(`):
  1. `scripts/seed_apollo_concept_registry.py:71` — `new = Subject(slug=slug,
     display_name=_human_name(slug))`. This is the WU-3B-adjacent seeder; it now must
     supply `search_space_id`. **WU-3A boundary:** add the keyword with a documented
     `# TODO(WU-3B): real per-course id` using the seeder's existing course context, OR,
     if the seeder has no course in scope, attribute to `MIN(aita_search_spaces.id)` to
     match the migration backfill. Do NOT redesign the seeder here — minimal edit to keep
     it constructing a valid `Subject`. (This file is outside the WU-3A scope list; touch
     ONLY this one line, and only because the NOT NULL column forces it. If the executor
     prefers, flag it for WU-3B instead and leave a failing-construction note — but a
     green suite needs the line fixed.)
  2. `apollo/overseer/tests/test_misconception_bank.py:47` — `subj =
     Subject(slug="generic_subject", display_name="Generic Subject")`. Add
     `search_space_id=TEST_SPACE_ID` (already imported pattern via `apollo.conftest`).
  No runtime (non-test, non-seeder) code constructs `Subject` — confirmed.

## Verification commands (ALL local — executor runs these)

Run from `ai-ta-backend/`. Docker MUST be running for the real-Postgres tests — a
SKIPPED DB suite does not satisfy this unit.

- [ ] Confirm Docker is up (the DB tests need it):
      `docker info >/dev/null 2>&1 && echo OK || echo "DOCKER DOWN — BLOCKER"`
- [ ] Real-Postgres migration behavior (the load-bearing suite — must show PASSED, not
      skipped): `pytest tests/database/test_apollo_learner_model_migration.py -v
      --tb=short`
- [ ] Fast ORM/metadata + allowlist tests:
      `pytest apollo/persistence/tests/test_learner_model_models.py
      apollo/persistence/tests/test_learner_model_allowlists.py -v`
- [ ] Full apollo + database suites stay green (catches the `Subject(search_space_id=…)`
      fixture break): `pytest apollo/ tests/database/ -q`
- [ ] Patch coverage gate (CLAUDE.md ≥95%, WU-3A base branch):
      `pytest --cov --cov-report=xml -q` then
      `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu2b-crossturn-writeedges
      --fail-under=95`
- [ ] Lint the edited model file (no SQL/migration linter is wired in this repo):
      `ruff check apollo/persistence/models.py` — expect no new warnings. The migration is
      validated behaviorally by File 1, not by a SQL linter.
- [ ] **STOP.** Executor's job ends here. No `supabase db push`, no `apply_migration`,
      no remote DDL.

## Deploy handoff (HUMAN/CI only — never executed by feller)

Migration 026 is applied to remote Supabase by a human/CI step, never by feller agents
(CLAUDE.md DB contract; memory `feedback_feller_db_local_only`). At deploy time:

1. **Resolve the number against the target project first.** 023 is a known prod/test
   collision and 024/025 are unapplied on `supabase-test` (prod applied-state
   unverified). Confirm the target project's applied migration set; if 024/025 are not
   yet applied there, apply them (and resolve the 023 collision) BEFORE 026, in order.
2. **Rehearse on `supabase-test` (hjevtxdtrkxjcaaexdxt) first**, then prod
   (uduxdniieeqbljtwocxy). The backfill Phase B (`MIN(search_space_id)`) is a bootstrap
   default; on a real project with multiple courses a human must verify each subject is
   attributed to the CORRECT course (or run the WU-3B seeder which sets the real mapping)
   — do not assume the lowest course is right for every subject in a multi-course project.
3. **Migration-before-code:** apply 026 before any later unit's code that reads the new
   tables deploys (same ordering rule as 023/024/025).
4. **Post-apply sanity (against the real DB, human-run):**
   `SELECT count(*) FROM apollo_subjects WHERE search_space_id IS NULL;` → expect 0;
   `\d+ apollo_learner_state` shows the composite PK and array CHECK;
   `SELECT conname FROM pg_constraint WHERE conname =
   'apollo_mastery_events_attempt_entity_kind_uniq';` → present.
5. **Advisor check:** run Supabase `get_advisors` (security + performance) after apply.

## Owner-doc updates

Both owner docs whose `owns:` globs cover the touched files are updated in the SAME work,
`last_verified` bumped to **2026-06-15**:

- **`docs/architecture/domain-data.md`** (owns `database/**`):
  - In the `database/migrations/` landmark row, append 026 to the migration list:
    "`026_apollo_learner_model.sql` adds the Apollo learner-model schema (course-scoping
    `apollo_subjects.search_space_id` with a 2-step backfill; Layer-1
    `apollo_kg_entities`/`apollo_entity_prereqs`; Layer-3 `apollo_learner_state`/
    `apollo_mastery_events` with `UNIQUE NULLS NOT DISTINCT`; grading-core
    `apollo_graph_comparison_runs`/`_findings`; `apollo_problem_attempts.
    learner_update_pending`). Guarded by
    `tests/database/test_apollo_learner_model_migration.py` (real-Postgres constraint
    behavior)."
  - Extend the existing "ORM declares no CHECK constraints, so
    `tests/database/test_teacher_uploads_constraints.py` and
    `tests/database/test_apollo_attempt_result_constraint.py` apply the real migration
    SQL on Testcontainers" sentence to add the new
    `test_apollo_learner_model_migration.py` as the third such test.
- **`docs/architecture/apollo.md`** (owns `apollo/**`):
  - In the `apollo/persistence/` landmark row, extend "SQLAlchemy models for all
    `apollo_*` Postgres tables" to name the 6 new learner-model models
    (`KGEntity`, `EntityPrereq`, `LearnerState`, `MasteryEvent`, `GraphComparisonRun`,
    `GraphComparisonFinding`) and the new allowlist constants (`ENTITY_KINDS`,
    `MASTERY_EVENT_KINDS`, `FINDING_KINDS`), plus the `Subject.search_space_id` /
    `ProblemAttempt.learner_update_pending` additions.
  - Add a one-line note: the Layer-1/Layer-3 schema now EXISTS (WU-3A, migration 026) but
    the resolver (`:Canon`/`RESOLVES_TO`), the bernoulli seed, the §3 update math, the §6
    grading core, and the §8A runtime cutover are LATER units — nothing reads/writes the
    new tables yet. (Prevents a reader assuming the learner model is wired.)

## Downstream consumers

WU-3A is purely additive schema; nothing reads the new tables yet. Grep before/after to
confirm no current consumer is silently affected:

```bash
grep -rn "apollo_kg_entities\|apollo_learner_state\|apollo_mastery_events\|apollo_graph_comparison" apollo/ database/ tests/ scripts/
grep -rn "learner_update_pending" apollo/ database/ tests/
grep -rn "\.search_space_id" apollo/persistence/ scripts/seed_apollo_concept_registry.py
```

Expected today: only the new migration, models, and tests match the new tables. The
`Subject` consumers (`scripts/seed_apollo_concept_registry.py:71`,
`apollo/overseer/tests/test_misconception_bank.py:47`) are the only existing code the
`search_space_id` NOT NULL column forces a touch on (see TDD plan).

Future consumers (out of scope, named for context only): WU-3B seeder writes these rows;
WU-3C resolver reads `apollo_kg_entities`; WU-3D §8A cutover reads `search_space_id` and
drops `concept_cluster_id`; the §3 update rule writes `apollo_learner_state`/
`apollo_mastery_events`; the §6 grading core writes the comparison tables.

## Risks

- **[HIGH] Real Postgres is mandatory; Docker may be unavailable.** The whole point of
  this unit (NULLS NOT DISTINCT, array CHECK, FK cascade) cannot be verified on SQLite.
  If the executor's environment cannot run Docker/Testcontainers, the unit is BLOCKED —
  do not SQLite-fake it, do not skip-and-pass. Mitigation: `docker info` precheck as the
  first verification step; a skipped DB suite is a failed gate.
- **[MEDIUM] Backfill default may mis-attribute subjects in a multi-course prod.** Phase
  B maps every orphan subject to `MIN(search_space_id)`. On a single-course pilot this is
  correct; on a multi-course project it is a placeholder the WU-3B seeder/human must
  correct. The migration guarantees NO NULL (so it applies), not that the mapping is
  semantically right. Called out in Deploy handoff step 2.
- **[MEDIUM] Migration numbering / 023 collision at deploy.** 024/025 unapplied on test,
  023 duplicated, prod unverified. 026 is independent of 024 (it depends on 018/023's
  `apollo_subjects`/`apollo_problem_attempts`/`aita_search_spaces`/`auth.users`), but the
  human MUST verify the target's applied set before applying. Header note + Deploy handoff
  step 1 cover this; it is a human/CI risk, not an executor one.
- **[MEDIUM] `Subject` NOT NULL break ripples to the seeder.**
  `scripts/seed_apollo_concept_registry.py` is outside the WU-3A scope file list but its
  one `Subject(...)` line must change or the suite (and the seeder) breaks. The plan
  bounds the edit to that single line; the recommended path is to make the one-line edit
  (alternative: flag for WU-3B, but then the full-suite gate shows it failing).
- **[LOW] `array_length(belief,1)=3` does not enforce sum-to-1.** By design (spec §2:
  "sums-to-1 validated app-side"). The CHECK only guards arity; the §3 update rule (later
  unit) guarantees normalization. Noted so no one adds a sum CHECK here.
- **[LOW] `learner_update_pending` server_default vs ORM default.** Both are set so raw
  SQL inserts and ORM inserts agree; a mismatch would surface as NULL on legacy-path rows.
  Covered by the raw-insert test and the ORM default test.
- **[LOW] `entity_id` cascade-vs-set-null asymmetry** (learner tables CASCADE, findings
  SET NULL; mastery_events `attempt_id` SET NULL) is intentional and easy to mis-copy.
  Each direction has a dedicated test in File 1.
- **[LOW] Lock duration / table size.** All target tables are tiny or empty in every
  environment (no Apollo learner data exists yet; `apollo_subjects` has at most a handful
  of rows). The `ALTER TABLE … ADD COLUMN` + backfill + `SET NOT NULL` on `apollo_subjects`
  and the `ADD COLUMN` on `apollo_problem_attempts` take a brief `ACCESS EXCLUSIVE` lock
  but on near-empty tables it is sub-second. No `NOT VALID`/`VALIDATE` split is needed at
  this scale; revisit only if these tables ever grow large (they will not before WU-3D).

## Out-of-scope boundaries for WU-3A

Explicitly NOT in this unit (do not build, even partially):

- **WU-3B** — the bernoulli seed/conversion script (`concept_dag.json` → entities, etc.),
  any data INSERT into the new tables beyond test fixtures, and the real per-course
  `search_space_id` mapping. WU-3A ships only the schema + the bootstrap backfill default.
- **WU-3C** — the resolver, the `:Canon` Neo4j projection, `RESOLVES_TO` edges, SymPy/
  alias/fuzzy tiers, misconception competition. No `apollo/retired graph comparator/` code.
- **WU-3D / §8A** — the runtime cutover: deleting `_AVAILABLE_CLUSTERS`/
  `_CLUSTER_TO_CONCEPT`, populating `apollo_sessions.concept_id`, dropping
  `concept_cluster_id`, making curriculum reads async, `load_concept` DB-backed. None of
  the §8A files (`session_init.py`, `problem_selector.py`, `concept_inference.py`,
  `subjects/__init__.py`, handler consumers) are touched.
- **§8B** — the materials → curriculum auto-provisioning pipeline.
- **The §3 update math / §6 grading core / §6.5 decision table** — no belief-update code,
  no simulation, no `events.py`/`findings.py`. The tables exist; nothing writes them yet.
- **No RLS POLICY authoring** beyond the `ENABLE ROW LEVEL SECURITY` stopgap mirroring
  migration 022 (per-table student/teacher policies are separate RLS work, not WU-3A).
- **No remote migration apply** (any project), no branch/PR/push, no Neo4j changes.

## Deviations I'd allow the executor

- **Splitting `models.py`** if it would exceed 800 lines after the 6 models (it will not,
  but if a future edit pushes it over, extract the learner-model models into
  `apollo/persistence/learner_models.py` and re-export from `models.py` so existing
  imports keep working).
- **Adjusting the content-scoped chain regex** in File 1 to include/exclude a migration,
  as long as the resulting pre-026 baseline creates `apollo_subjects`, `apollo_concepts`,
  `apollo_problem_attempts`, and the FK-stub targets. The named set (009/018/019/023/025)
  is the expected result, not a hard-coded list to copy if the regex finds the same set.
- **Backfill attribution rule:** `MIN(aita_search_spaces.id)` is the documented default;
  the executor MAY use a clearer deterministic rule (e.g. an explicit course id in a
  single-course fixture) PROVIDED the populated-DB, empty-DB, and guard-raise tests all
  still pass and no subject is left NULL.
- **Extra defensive CHECKs** (e.g. `evidence_count >= 0`) are allowed if added with a
  matching reject/accept test; do NOT add a `belief` sum-to-1 CHECK (spec says app-side).
- **`server_default` form** for `learner_update_pending` (`text("false")` vs
  `text("'false'")`) — either valid Postgres boolean default is fine; keep the ORM
  `default=False` in sync.
- **NOT negotiable:** real-Postgres tests over SQLite for DDL behavior; per-concept (not
  global) `canonical_key` uniqueness; the 2-step backfill before NOT NULL; NULLS NOT
  DISTINCT on the mastery-events dedup; the reconciliation header note; no remote apply;
  updating BOTH owner docs with `last_verified: 2026-06-15`.
