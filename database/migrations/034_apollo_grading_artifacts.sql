-- 034_apollo_grading_artifacts.sql
-- Canonical grading artifact (spec 2026-07-01 §1): ONE immutable record per
-- Done-click per grader role. role='canonical' is the record of the grade the
-- student was served; role='pair' is the other grader's artifact captured on
-- the same input (paired-artifact contract, spec §5). Append-only: no UPDATE
-- path exists in code; retuning weights affects future artifacts only.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_grading_artifacts (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL
        REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('canonical', 'pair')),
    grader_used TEXT NOT NULL CHECK (grader_used IN ('graph', 'llm_fallback')),
    user_id UUID NOT NULL,
    search_space_id BIGINT NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    problem_id TEXT NOT NULL,
    versions JSONB NOT NULL,            -- {grader, reference_graph_hash, nli_model, weights_version}
    node_ledger JSONB NOT NULL,         -- [{key, status, method, confidence, evidence_span}]
    edge_ledger JSONB NOT NULL,         -- [{key, edge_type, status, method, confidence, evidence_span}]
    misconceptions JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{key, utterance, confidence}]
    clarification_trace JSONB NOT NULL DEFAULT '[]'::jsonb,
    scores JSONB NOT NULL,              -- {node_coverage, edge_coverage, misconception_penalty,
                                        --  composite, weights:{w_n,w_e,p}}
    abstention JSONB,                   -- null OR {reasons:[...], llm_fallback_grade,
                                        --  graph_failure: str|null}
    grading_latency_ms INTEGER,         -- Done-click grading latency (ops gate input)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_grading_artifact_attempt_role UNIQUE (attempt_id, role)
);

CREATE INDEX IF NOT EXISTS ix_grading_artifacts_space_concept_time
    ON apollo_grading_artifacts (search_space_id, concept_id, created_at);
CREATE INDEX IF NOT EXISTS ix_grading_artifacts_user
    ON apollo_grading_artifacts (user_id, created_at);

-- RLS stopgap (mirror migrations 022/026/033): default-deny to PostgREST, no
-- policies. The owner-connection backend is exempt; the anon/public key cannot
-- read/write. App-layer tenant scoping (auth.py + search_space_id) enforces.
ALTER TABLE apollo_grading_artifacts ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_grading_artifacts IS
    'Canonical grading artifact (spec 2026-07-01 §1): one immutable row per '
    'Done-click per grader role. role=canonical is what the student was '
    'served; role=pair is the other graders artifact captured on the same '
    'input (paired-artifact contract, spec §5). Append-only.';

COMMIT;
