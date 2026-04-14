-- 009_apollo_slice0.sql
-- Apollo v2 Slice 0 persistence tables: sessions, KG entries, messages, problem attempts.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_sessions (
    id                   BIGSERIAL PRIMARY KEY,
    student_id           TEXT       NOT NULL,
    concept_cluster_id   TEXT       NOT NULL,
    status               TEXT       NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'paused', 'ended')),
    phase                TEXT       NOT NULL DEFAULT 'INIT'
                         CHECK (phase IN ('INIT','TEACHING','PROBLEM_REVEAL','SOLVING','REPORT','BETWEEN')),
    current_problem_id   TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_touched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_sessions_student ON apollo_sessions(student_id);

-- Enforce one active session per student at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ix_apollo_sessions_unique_active_per_student
    ON apollo_sessions(student_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS apollo_kg_entries (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT      NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    type         TEXT        NOT NULL
                 CHECK (type IN ('equation','definition','condition','simplification','variable_mapping')),
    content      JSONB       NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'parser'
                 CHECK (source IN ('parser', 'student_edit')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_kg_entries_session ON apollo_kg_entries(session_id);

CREATE TABLE IF NOT EXISTS apollo_messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT      NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    role         TEXT        NOT NULL CHECK (role IN ('student', 'apollo', 'system')),
    content      TEXT        NOT NULL,
    turn_index   INTEGER     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_messages_session ON apollo_messages(session_id);

CREATE TABLE IF NOT EXISTS apollo_problem_attempts (
    id                   BIGSERIAL PRIMARY KEY,
    session_id           BIGINT      NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    problem_id           TEXT        NOT NULL,
    difficulty           TEXT        NOT NULL CHECK (difficulty IN ('intro', 'standard', 'hard')),
    result               TEXT
                         CHECK (result IS NULL OR result IN ('solved', 'stuck', 'skipped', 'returned_to_hoot')),
    solver_trace         JSONB,
    diagnostic_report    JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_problem_attempts_session ON apollo_problem_attempts(session_id);

COMMIT;
