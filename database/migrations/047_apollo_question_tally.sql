BEGIN;

CREATE TABLE IF NOT EXISTS apollo_question_tally (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    reference_node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]',
    student_declined BOOLEAN NOT NULL DEFAULT FALSE,
    times_asked INTEGER NOT NULL DEFAULT 0,
    last_asked_turn INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, reference_node_id)
);

CREATE INDEX IF NOT EXISTS ix_apollo_question_tally_attempt
    ON apollo_question_tally (attempt_id);

ALTER TABLE apollo_question_tally ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_question_tally IS
    'Durable per-attempt memory for Apollo reference-node questioning judgments.';

COMMIT;
