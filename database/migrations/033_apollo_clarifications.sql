-- 033_apollo_clarifications.sql
-- Apollo clarification loop (G2): one row per ambiguous student idea Apollo
-- probed with an answer-blind follow-up. State machine:
--   asked_waiting -> {confirmed | refuted | vague}  (terminal; one probe per idea)
-- A `confirmed` row resolves the node at grading via the `clarification` method
-- (cap 0.90). A `refuted` row is misconception evidence (no credit). RLS follows
-- the Apollo default-deny stopgap (mirror 022/026): ENABLE, no policies, the
-- owner-connection backend is exempt; app-layer scoping is search_space_id.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_clarifications (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id         BIGINT NOT NULL
        REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    session_id         BIGINT NOT NULL
        REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    user_id            UUID NOT NULL
        REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id         BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    node_id            TEXT NOT NULL,
    candidate_key      TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'asked_waiting'
        CHECK (state IN ('asked_waiting', 'confirmed', 'refuted', 'vague')),
    probe_question     TEXT NOT NULL,
    original_statement TEXT NOT NULL,
    clarification_text TEXT,
    asked_turn         INTEGER NOT NULL,
    answered_turn      INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One follow-up per idea per attempt (spec §10 state machine).
    CONSTRAINT apollo_clarifications_attempt_node_uniq UNIQUE (attempt_id, node_id)
);

CREATE INDEX IF NOT EXISTS apollo_clarifications_attempt_state_idx
    ON apollo_clarifications(attempt_id, state);

CREATE INDEX IF NOT EXISTS apollo_clarifications_user_concept_idx
    ON apollo_clarifications(user_id, concept_id);

-- RLS stopgap (mirror migrations 022/026): default-deny to PostgREST, no
-- policies. The owner-connection backend is exempt; the anon/public key cannot
-- read/write. App-layer tenant scoping (auth.py + search_space_id) enforces.
ALTER TABLE apollo_clarifications ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_clarifications IS
    'Apollo clarification loop (G2): one row per ambiguous student idea probed '
    'with an answer-blind follow-up; confirmed rows resolve at grading via the '
    'clarification method (0.90), refuted rows are misconception evidence.';

COMMIT;
