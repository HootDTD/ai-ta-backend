-- 037_apollo_misconception_observations.sql
-- Emergent per-class misconception store (design memo 2026-07-05, increment 1;
-- lane B3b/D1). Append-only observation ledger: ONE row per
-- (attempt_id, signature) derived from a role='canonical' grading artifact's
-- asserted misconceptions, written only when the APOLLO_EMERGENT_MISCONCEPTIONS
-- flag is ON. The ledger is the source of truth; trust is derived on read
-- (apollo/emergent/store.py), never materialized here (increment 2 defers the
-- rolled-up bank + teacher-curation columns).
--
-- Numbering: on-disk max was 035; 036 is claimed by a sibling lane's PR, so
-- this store takes 037 (the memo proposed 036 pre-collision).
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_misconception_observations (
    id BIGSERIAL PRIMARY KEY,
    search_space_id BIGINT NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,   -- P5 per-class isolation
    concept_id BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    signature TEXT NOT NULL,            -- canonical_key OR 'unkeyed:<concept_id>' bucket
    user_id UUID NOT NULL,              -- distinct-student counting (trust support term)
    attempt_id BIGINT
        REFERENCES apollo_problem_attempts(id) ON DELETE SET NULL,  -- log outlives the attempt
    confidence REAL,                    -- resolver confidence of the asserting finding
    opposes TEXT,                       -- reference node the misconception contradicts (or NULL)
    evidence_span TEXT,                 -- representative student utterance
    source TEXT NOT NULL DEFAULT 'grading_artifact',  -- future: 'clarification_refuted'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- P6 idempotency: a re-grade / retry of the same attempt cannot double-count.
    -- Mirrors apollo_mastery_events' (attempt_id, ...) UNIQUE. Only role=canonical
    -- artifacts feed this table, so (attempt_id, signature) is the per-attempt key.
    CONSTRAINT uq_misconception_observation_attempt_signature
        UNIQUE (attempt_id, signature)
);

-- Read path scans one class+concept and GROUPs BY signature; the PK leads with
-- id so it cannot serve that scan. Mirrors ix_grading_artifacts_space_concept_time.
CREATE INDEX IF NOT EXISTS ix_apollo_misconception_obs_space_concept
    ON apollo_misconception_observations (search_space_id, concept_id);

-- RLS stopgap (mirror migrations 022/026/033/034): default-deny to PostgREST,
-- no policies. The owner-connection backend is exempt; the anon/public key
-- cannot read/write. App-layer tenant scoping (auth.py + search_space_id)
-- enforces tenancy.
ALTER TABLE apollo_misconception_observations ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_misconception_observations IS
    'Emergent per-class misconception store (memo 2026-07-05, increment 1): '
    'append-only observation ledger, one row per (attempt_id, signature) from '
    'role=canonical grading artifacts. Written only when '
    'APOLLO_EMERGENT_MISCONCEPTIONS is ON. Trust is derived on read, not stored. '
    'Append-only; the source of truth for a rebuildable increment-2 bank.';

COMMIT;
