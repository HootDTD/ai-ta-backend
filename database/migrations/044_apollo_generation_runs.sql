-- GEN-4: teacher-initiated problem-generation batch tracking.
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_generation_runs (
    id                  BIGSERIAL PRIMARY KEY,
    search_space_id     INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id          BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','succeeded','failed')),
    result_summary      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingest_run_id       BIGINT REFERENCES apollo_ingest_runs(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS apollo_generation_runs_space_idx
    ON apollo_generation_runs(search_space_id);

CREATE INDEX IF NOT EXISTS apollo_generation_runs_concept_idx
    ON apollo_generation_runs(concept_id);

ALTER TABLE apollo_generation_runs ENABLE ROW LEVEL SECURITY;

COMMIT;
