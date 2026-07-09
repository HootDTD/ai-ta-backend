-- 013_apollo_student_progress.sql
-- Apollo teaching-rigor phase 2 (gamification): persistent XP + level per
-- student, keyed by the same student_id string stored on apollo_sessions.
-- Kept in a side table so gamification state is isolated from identity /
-- auth state and can be reset without touching core student records.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_student_progress (
    student_id        TEXT        PRIMARY KEY,
    xp_total          INTEGER     NOT NULL DEFAULT 0,
    level             INTEGER     NOT NULL DEFAULT 1,
    last_level_up_at  TIMESTAMPTZ NULL
);

COMMIT;
