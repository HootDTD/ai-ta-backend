-- 018_apollo_concept_registry.sql
-- Class 2 Phase 2: move the per-subject / per-concept registry from the
-- filesystem (apollo/subjects/<subject>/concepts/<concept>/*.json) into
-- Postgres so curriculum content is data, not code.
--
-- Rationale: the first real class will not be fluid mechanics. Hardcoding
-- subject/concept strings into Python (cluster_to_concept maps,
-- load_concept(subject_id, concept_id) signatures) couples the runtime to
-- the curriculum and forces a code deploy for every new class. After
-- this migration, adding a class is one INSERT per subject + N INSERTs
-- per concept, runnable from the teacher UI or a seeder script.
--
-- New tables:
--   apollo_subjects             — top-level curriculum domain
--   apollo_concepts             — one row per teachable concept; the per-
--                                 concept artifacts (canonical_symbols,
--                                 forbidden_named_laws, parser_prompt_template,
--                                 solver_hints, normalization_map) live as
--                                 JSONB / TEXT columns
--   apollo_concept_problems     — per-concept problem bank
--
-- Modified tables:
--   apollo_sessions             — concept_cluster_id (TEXT) replaced by
--                                 concept_id (FK BIGINT)

BEGIN;

-- -----------------------------------------------------------------------------
-- apollo_subjects
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apollo_subjects (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE apollo_subjects IS
    'Top-level curriculum domain (e.g. fluid_mechanics, thermodynamics, organic_chemistry).';
COMMENT ON COLUMN apollo_subjects.slug IS
    'Stable, machine-readable identifier. Matches the legacy filesystem dir name during migration.';

-- -----------------------------------------------------------------------------
-- apollo_concepts
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apollo_concepts (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id              BIGINT NOT NULL REFERENCES apollo_subjects(id) ON DELETE CASCADE,
    slug                    TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    -- Concept-shaped curriculum payloads — verbatim from the registry's
    -- per-concept JSON / Markdown files. Pydantic models continue to
    -- validate these on load; storing them as JSONB / TEXT keeps the
    -- migration additive and lossless.
    canonical_symbols       JSONB NOT NULL DEFAULT '{}'::jsonb,
    normalization_map       JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_prompt_template  TEXT  NOT NULL DEFAULT '',
    solver_hints            JSONB NOT NULL DEFAULT '{}'::jsonb,
    forbidden_named_laws    JSONB NOT NULL DEFAULT '{}'::jsonb,
    concept_dag             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_id, slug)
);

CREATE INDEX IF NOT EXISTS apollo_concepts_subject_idx
    ON apollo_concepts(subject_id);

COMMENT ON TABLE apollo_concepts IS
    'One row per teachable concept. Replaces the filesystem layout '
    'apollo/subjects/<subject>/concepts/<concept>/*.json. Curriculum '
    'authors edit rows here (not code) to add or revise content.';

-- -----------------------------------------------------------------------------
-- apollo_concept_problems
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apollo_concept_problems (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id    BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    -- Author-facing identifier — stable across edits, used by tests, logs,
    -- and ProblemAttempt.problem_id.
    problem_code  TEXT NOT NULL,
    difficulty    TEXT NOT NULL,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_id, problem_code)
);

CREATE INDEX IF NOT EXISTS apollo_concept_problems_concept_difficulty_idx
    ON apollo_concept_problems(concept_id, difficulty);

COMMENT ON TABLE apollo_concept_problems IS
    'Per-concept problem bank. Replaces apollo/subjects/.../problems/*.json. '
    'payload is the full Problem schema (problem_text, given_values, '
    'target_unknown, reference_solution).';

-- -----------------------------------------------------------------------------
-- apollo_sessions: concept_cluster_id (TEXT) -> concept_id (FK BIGINT)
-- -----------------------------------------------------------------------------
ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS concept_id BIGINT
        REFERENCES apollo_concepts(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS apollo_sessions_concept_id_idx
    ON apollo_sessions(concept_id);

COMMENT ON COLUMN apollo_sessions.concept_id IS
    'FK into apollo_concepts. Replaces concept_cluster_id (TEXT). '
    'Backfill happens in the seeder once concept rows exist; '
    'concept_cluster_id stays during the cutover and is dropped in 022.';

COMMIT;
