-- 014_apollo_attempt_id.sql
-- Migrate apollo_kg_entries and apollo_messages from session-scoped to
-- attempt-scoped. Adds nullable attempt_id columns with FK + cascade to
-- apollo_problem_attempts, backfills from the single existing attempt per
-- session (true for all current data because sessions have been
-- single-problem to date), and indexes the new columns.

BEGIN;

ALTER TABLE apollo_kg_entries
    ADD COLUMN IF NOT EXISTS attempt_id BIGINT
    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE;

ALTER TABLE apollo_messages
    ADD COLUMN IF NOT EXISTS attempt_id BIGINT
    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE;

UPDATE apollo_kg_entries
SET attempt_id = (
    SELECT id FROM apollo_problem_attempts
    WHERE session_id = apollo_kg_entries.session_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE attempt_id IS NULL;

UPDATE apollo_messages
SET attempt_id = (
    SELECT id FROM apollo_problem_attempts
    WHERE session_id = apollo_messages.session_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE attempt_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_apollo_kg_entries_attempt_id
    ON apollo_kg_entries(attempt_id);

CREATE INDEX IF NOT EXISTS ix_apollo_messages_attempt_id
    ON apollo_messages(attempt_id);

COMMIT;
