-- 032_apollo_authored_sets.sql — paired authored problem/solution sets (WU-AAS).
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_authored_sets (
    id                    BIGSERIAL PRIMARY KEY,
    search_space_id       INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    set_index             INTEGER NOT NULL,
    problem_document_id   BIGINT,
    solution_document_id  BIGINT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','indexing','provisioning','done','failed')),
    result_summary        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (search_space_id, set_index)
);

CREATE INDEX IF NOT EXISTS apollo_authored_sets_space_idx
    ON apollo_authored_sets(search_space_id);

COMMIT;
