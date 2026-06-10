-- 023_apollo_auth_scoping.sql
-- Learner-model Phase 1 (spec: 2026-06-10-apollo-kg-learner-model-architecture-decision.md §8).
-- Closes the security.md "Known gaps" item: apollo_* tables keyed by loose
-- student_id TEXT with no course scoping. Re-keys apollo_sessions by
-- (user_id UUID -> auth.users, search_space_id -> aita_search_spaces) and
-- apollo_student_progress by user_id UUID (XP stays global per student).
--
-- DESTRUCTIVE: rows whose student_id is not a real auth.users UUID, or whose
-- user has no course membership to attribute a search_space_id from, are
-- dev/test artifacts and are deleted. Verified near-empty in prod before
-- applying (plan Task 1 Step 1). Rehearse on the test project first.

BEGIN;

-- ---------------------------------------------------------------------------
-- apollo_sessions: identity + course scoping
-- ---------------------------------------------------------------------------
ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS user_id UUID,
    ADD COLUMN IF NOT EXISTS search_space_id INTEGER;

-- The student UI has always sent the Supabase auth user id as student_id.
UPDATE apollo_sessions
SET user_id = student_id::uuid
WHERE user_id IS NULL
  AND student_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

-- Best-effort course attribution from the user's membership (legacy rows
-- predate course scoping; pilot users belong to one course).
UPDATE apollo_sessions s
SET search_space_id = cm.search_space_id
FROM course_memberships cm
WHERE s.search_space_id IS NULL
  AND s.user_id IS NOT NULL
  AND cm.user_id = s.user_id;

DELETE FROM apollo_sessions WHERE user_id IS NULL OR search_space_id IS NULL;

ALTER TABLE apollo_sessions
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN search_space_id SET NOT NULL,
    ADD CONSTRAINT apollo_sessions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE,
    ADD CONSTRAINT apollo_sessions_search_space_id_fkey
        FOREIGN KEY (search_space_id) REFERENCES aita_search_spaces(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_apollo_sessions_user_id
    ON apollo_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_apollo_sessions_search_space_id
    ON apollo_sessions(search_space_id);

-- One active session per student (same semantics as before, new key).
DROP INDEX IF EXISTS ix_apollo_sessions_unique_active_per_student;
CREATE UNIQUE INDEX ix_apollo_sessions_unique_active_per_user
    ON apollo_sessions(user_id) WHERE status = 'active';

DROP INDEX IF EXISTS ix_apollo_sessions_student;
ALTER TABLE apollo_sessions DROP COLUMN student_id;

-- ---------------------------------------------------------------------------
-- apollo_student_progress: re-key by auth UUID (XP stays global per student)
-- ---------------------------------------------------------------------------
ALTER TABLE apollo_student_progress
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE apollo_student_progress
SET user_id = student_id::uuid
WHERE user_id IS NULL
  AND student_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

DELETE FROM apollo_student_progress WHERE user_id IS NULL;

ALTER TABLE apollo_student_progress
    ALTER COLUMN user_id SET NOT NULL,
    DROP CONSTRAINT apollo_student_progress_pkey,
    ADD PRIMARY KEY (user_id),
    DROP COLUMN student_id;

COMMIT;
