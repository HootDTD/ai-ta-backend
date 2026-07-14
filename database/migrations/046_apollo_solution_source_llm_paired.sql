-- Authored-set structure pairing: preserve LLM-paired solution provenance.
--
-- Migration 030 introduced the solution_source domain as an inline CHECK,
-- which PostgreSQL named apollo_concept_problems_solution_source_check.
-- Structure-paired references pass the same verification and pairing gates as
-- extracted references, but retain their distinct origin through promotion.
--
-- Guarded on column existence: the migration-036 test harness replays every
-- migration that touches apollo_concept_problems BEFORE 030 (which mints the
-- column), so this must be a no-op until 030 has run. Real chains apply in
-- numeric order and always take the guarded ALTER path.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'apollo_concept_problems'
          AND column_name = 'solution_source'
    ) THEN
        ALTER TABLE apollo_concept_problems
            DROP CONSTRAINT IF EXISTS apollo_concept_problems_solution_source_check;

        ALTER TABLE apollo_concept_problems
            ADD CONSTRAINT apollo_concept_problems_solution_source_check
            CHECK (
                solution_source IS NULL
                OR solution_source IN ('extracted', 'generated', 'authored', 'llm_paired')
            );
    END IF;
END $$;

COMMIT;
