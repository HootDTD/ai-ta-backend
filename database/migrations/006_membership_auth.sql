-- 006_membership_auth.sql
-- Course memberships + RLS policies for secure multi-user isolation.

BEGIN;

CREATE TABLE IF NOT EXISTS course_memberships (
    user_id          UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id  INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    role             TEXT    NOT NULL CHECK (role IN ('student', 'teacher')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, search_space_id)
);

CREATE INDEX IF NOT EXISTS idx_course_memberships_search_space
    ON course_memberships (search_space_id, role);

ALTER TABLE course_memberships ENABLE ROW LEVEL SECURITY;

-- RLS policies
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'course_memberships'
          AND policyname = 'course_memberships_self_read'
    ) THEN
        CREATE POLICY course_memberships_self_read
            ON course_memberships FOR SELECT
            USING (user_id = auth.uid());
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'chat_sessions'
          AND policyname = 'chat_sessions_owner_rw'
    ) THEN
        CREATE POLICY chat_sessions_owner_rw
            ON chat_sessions FOR ALL
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid());
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'chat_turns'
          AND policyname = 'chat_turns_owner_rw'
    ) THEN
        CREATE POLICY chat_turns_owner_rw
            ON chat_turns FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM chat_sessions cs
                    WHERE cs.id = chat_turns.chat_session_id
                      AND cs.user_id = auth.uid()
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM chat_sessions cs
                    WHERE cs.id = chat_turns.chat_session_id
                      AND cs.user_id = auth.uid()
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'teacher_courses'
          AND policyname = 'teacher_courses_teacher_rw'
    ) THEN
        CREATE POLICY teacher_courses_teacher_rw
            ON teacher_courses FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_courses.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_courses.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'teacher_uploads'
          AND policyname = 'teacher_uploads_teacher_rw'
    ) THEN
        CREATE POLICY teacher_uploads_teacher_rw
            ON teacher_uploads FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_uploads.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_uploads.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            );
    END IF;
END $$;

COMMIT;
