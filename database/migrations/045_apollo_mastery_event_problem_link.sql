BEGIN;

ALTER TABLE apollo_mastery_events
    ADD COLUMN IF NOT EXISTS concept_problem_id BIGINT
        REFERENCES apollo_concept_problems(id) ON DELETE SET NULL;

COMMENT ON COLUMN apollo_mastery_events.concept_problem_id IS
    'Problem-bank row this outcome came from (resolved at write time from '
    '(session.concept_id, attempt.problem_code)); NULL when unresolvable. '
    'The per-item difficulty-calibration key (GEN-5).';

CREATE INDEX IF NOT EXISTS apollo_mastery_events_problem_created_idx
    ON apollo_mastery_events (concept_problem_id, created_at)
    WHERE concept_problem_id IS NOT NULL;

COMMIT;
