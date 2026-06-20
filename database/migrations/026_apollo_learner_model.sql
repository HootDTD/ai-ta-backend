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

-- ===========================================================================
-- 1. apollo_subjects.search_space_id (course-scoping, two-step safe backfill)
-- ===========================================================================

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

-- ===========================================================================
-- 2. Layer 1 — apollo_kg_entities + apollo_entity_prereqs
-- ===========================================================================

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
    -- is two distinct concepts -> two distinct entity rows. Never UNIQUE(canonical_key).
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

-- ===========================================================================
-- 3. Layer 3 — apollo_learner_state (current snapshot)
-- ===========================================================================

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

-- ===========================================================================
-- 4. Layer 3 — apollo_mastery_events (append-only log)
-- ===========================================================================

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

-- ===========================================================================
-- 5. Grading-core audit — apollo_graph_comparison_runs + _findings
-- ===========================================================================

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

-- ===========================================================================
-- 6. apollo_problem_attempts.learner_update_pending
-- ===========================================================================

-- §6/§7 cross-store retry flag: set true when the learner update is deferred (resolution
-- infra failure) so a janitor/next-session retry re-runs from resolution idempotently.
ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_pending BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN apollo_problem_attempts.learner_update_pending IS
    'True iff a Done-time learner-model update is pending retry (spec §5/§6/§7). The '
    'grade is never voided by grading-pipeline failure; this flag covers the cross-store '
    'window.';

-- ===========================================================================
-- 7. RLS stopgap (mirror migration 022): default-deny to PostgREST on the 6 new
--    public tables. NO policies — the owner-connection backend is exempt; the
--    anon/public key cannot read/write. App-layer tenant scoping (auth.py +
--    search_space_id predicate) is the enforcement point.
-- ===========================================================================

ALTER TABLE apollo_kg_entities               ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_entity_prereqs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_learner_state             ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_mastery_events            ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_graph_comparison_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_graph_comparison_findings ENABLE ROW LEVEL SECURITY;

COMMIT;

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
