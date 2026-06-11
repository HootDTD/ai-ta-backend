-- 024_teacher_textbook.sql
-- Allow course-wide textbook uploads in teacher_uploads.
--
-- The textbook upload path writes kind='textbook' with the COURSE_WIDE_WEEK
-- sentinel (week=0, see knowledge/teacher_weekly.py). The original 004 checks
-- only admit weekly materials (kind notes/slides, week 1..16), so the INSERT
-- raises check_violation on any database where they are live. Relax both.
--
-- MUST be applied before the textbook backend code is deployed (same
-- migration-before-code ordering as 023). Idempotent: drop-if-exists + add.
--
-- The partial unique index uniq_teacher_uploads_latest
-- (search_space_id, week, kind) WHERE is_latest stays correct with week=0
-- fixed: exactly one latest textbook per course.

BEGIN;

ALTER TABLE teacher_uploads
    DROP CONSTRAINT IF EXISTS teacher_uploads_week_check;
ALTER TABLE teacher_uploads
    ADD CONSTRAINT teacher_uploads_week_check
    CHECK (week BETWEEN 0 AND 16);

ALTER TABLE teacher_uploads
    DROP CONSTRAINT IF EXISTS teacher_uploads_kind_check;
ALTER TABLE teacher_uploads
    ADD CONSTRAINT teacher_uploads_kind_check
    CHECK (kind IN ('notes', 'slides', 'textbook'));

COMMIT;
