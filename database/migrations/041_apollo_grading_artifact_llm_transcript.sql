-- Part 1 transcript grader: make the future canonical artifact value legal.
-- File-only migration; artifact writing remains on graph/llm_fallback until calibration passes.
BEGIN;
ALTER TABLE apollo_grading_artifacts
    DROP CONSTRAINT IF EXISTS apollo_grading_artifacts_grader_used_check;
ALTER TABLE apollo_grading_artifacts
    ADD CONSTRAINT apollo_grading_artifacts_grader_used_check
    CHECK (grader_used IN ('graph', 'llm_fallback', 'llm_transcript'));
COMMIT;
