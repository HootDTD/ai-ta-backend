-- 048_apollo_typed_rehoming_jobs.sql
-- Durable, retryable post-promotion concept re-homing for manual typed drafts.
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_rehoming_jobs (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    search_space_id       INTEGER NOT NULL
                              REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_problem_id    BIGINT NOT NULL
                              REFERENCES apollo_concept_problems(id) ON DELETE CASCADE,
    requested_concept_id  BIGINT
                              REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    state                 TEXT NOT NULL DEFAULT 'pending'
                              CHECK (state IN ('pending','running','completed','failed')),
    lease_owner           TEXT,
    lease_expires_at      TIMESTAMPTZ,
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS apollo_rehoming_jobs_open_problem_uniq
    ON apollo_rehoming_jobs(concept_problem_id)
    WHERE state IN ('pending','running');

CREATE INDEX IF NOT EXISTS apollo_rehoming_jobs_claim_idx
    ON apollo_rehoming_jobs(state, lease_expires_at, created_at);

ALTER TABLE apollo_rehoming_jobs ENABLE ROW LEVEL SECURITY;

COMMIT;
