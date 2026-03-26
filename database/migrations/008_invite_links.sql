-- 008_invite_links.sql
-- Shareable invite links for course enrollment (student + teacher roles).

BEGIN;

CREATE TABLE IF NOT EXISTS course_invite_links (
    id              BIGSERIAL   PRIMARY KEY,
    code            TEXT        NOT NULL UNIQUE,
    search_space_id INTEGER     NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('student', 'teacher')),
    created_by      UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    max_uses        INTEGER     DEFAULT NULL,
    use_count       INTEGER     NOT NULL DEFAULT 0,
    expires_at      TIMESTAMPTZ DEFAULT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_invite_links_code
    ON course_invite_links (code) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_invite_links_space
    ON course_invite_links (search_space_id);

ALTER TABLE course_invite_links ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'course_invite_links'
          AND policyname = 'invite_links_teacher_rw'
    ) THEN
        CREATE POLICY invite_links_teacher_rw
            ON course_invite_links FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = course_invite_links.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = course_invite_links.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            );
    END IF;
END $$;

COMMIT;
