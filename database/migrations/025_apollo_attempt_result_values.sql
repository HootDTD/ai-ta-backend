-- 025_apollo_attempt_result_values.sql
-- Widen the apollo_problem_attempts.result CHECK constraint.
--
-- Root cause (staging 500 on Apollo "I'm finished teaching", 2026-06-15):
-- migration 009 created
--     CHECK (result IS NULL OR result IN
--            ('solved','stuck','skipped','returned_to_hoot'))
-- but the code has since drifted to write two values that were never added:
--   * handle_done  -> result='graded'    (commit 21b42e1 dropped the SymPy
--                                          solver; diff+rubric is now the grade)
--   * handle_next  -> result='abandoned' (prior attempt on a mid-problem switch)
-- Every Done (and every mid-problem switch) therefore raised
-- asyncpg.CheckViolationError -> HTTP 500 after the grade had been computed.
--
-- The new allowlist is a strict SUPERSET of the old one, so it validates
-- against all existing rows with no data backfill. The set here MUST match
-- apollo.persistence.models.ATTEMPT_RESULTS (single source of truth, asserted
-- by apollo/persistence/tests/test_attempt_result_constraint.py).
--
-- MUST be applied before this code is deployed to a given environment
-- (migration-before-code ordering, same as 023/024). Idempotent:
-- drop-if-exists + add.

BEGIN;

ALTER TABLE apollo_problem_attempts
    DROP CONSTRAINT IF EXISTS apollo_problem_attempts_result_check;

ALTER TABLE apollo_problem_attempts
    ADD CONSTRAINT apollo_problem_attempts_result_check
    CHECK (
        result IS NULL
        OR result IN (
            'solved',
            'stuck',
            'skipped',
            'returned_to_hoot',
            'abandoned',
            'graded'
        )
    );

COMMIT;
