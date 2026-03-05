-- 004_teacher_features.sql
-- Supabase-first teacher weekly features keyed by aita_search_spaces.id.

BEGIN;

CREATE TABLE IF NOT EXISTS teacher_courses (
    id              BIGSERIAL PRIMARY KEY,
    search_space_id INTEGER NOT NULL UNIQUE
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    current_week    INTEGER NOT NULL DEFAULT 1 CHECK (current_week BETWEEN 1 AND 16),
    weights         JSONB   NOT NULL DEFAULT '{}'::jsonb,
    weight_bounds   JSONB   NOT NULL DEFAULT '{"min":0.0,"max":1.0}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teacher_courses_search_space_id
    ON teacher_courses (search_space_id);

CREATE TABLE IF NOT EXISTS teacher_uploads (
    id              BIGSERIAL PRIMARY KEY,
    search_space_id INTEGER NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    week            INTEGER NOT NULL CHECK (week BETWEEN 1 AND 16),
    kind            TEXT    NOT NULL CHECK (kind IN ('notes', 'slides')),
    title           TEXT    NOT NULL,
    source_name     TEXT,
    doc_id          INTEGER
        REFERENCES aita_documents(id) ON DELETE SET NULL,
    page_count      INTEGER,
    is_latest       BOOLEAN NOT NULL DEFAULT TRUE,
    uploaded_by     UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    metadata        JSONB   NOT NULL DEFAULT '{}'::jsonb,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teacher_uploads_lookup
    ON teacher_uploads (search_space_id, week, kind, uploaded_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_teacher_uploads_latest
    ON teacher_uploads (search_space_id, week, kind)
    WHERE is_latest = TRUE;

ALTER TABLE teacher_courses ENABLE ROW LEVEL SECURITY;
ALTER TABLE teacher_uploads ENABLE ROW LEVEL SECURITY;

COMMIT;
