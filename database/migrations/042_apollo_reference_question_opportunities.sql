BEGIN;

CREATE TABLE IF NOT EXISTS apollo_reference_question_opportunities (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    session_id BIGINT NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    reference_node_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'asked_waiting'
        CHECK (state IN ('asked_waiting', 'answered')),
    question TEXT NOT NULL,
    asked_turn INTEGER NOT NULL,
    answered_turn INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT apollo_reference_question_opportunities_attempt_node_uniq
        UNIQUE (attempt_id, reference_node_id)
);

CREATE INDEX IF NOT EXISTS apollo_reference_question_opportunities_attempt_state_idx
    ON apollo_reference_question_opportunities(attempt_id, state);

ALTER TABLE apollo_reference_question_opportunities ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_reference_question_opportunities IS
    'One answer-blind Apollo question opportunity per authored reference node and attempt.';

COMMIT;
