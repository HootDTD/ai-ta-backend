-- 028_apollo_learner_janitor.sql  (next_free = 028; on-disk tops at 027)
-- WU-5B3a — dead-letter + exponential-backoff columns for the learner_update
-- retry janitor. NO graded_at column: the original done_ts is durably on every
-- frozen Neo4j node (store.stamp_graded_at), read back via read_node_graded_at.
-- The janitor's claim/recompute state machine (WU-5B3a-1) CONSUMES these columns;
-- WU-5B3a-0 ships the DDL + ORM mapping only (nothing reads them yet).
--
-- LOCAL-DOCKER-ONLY: applied to local Docker Postgres by the feller migration
-- tests. NEVER applied to any remote Supabase project by an agent — deploying
-- (test rehearsal, then prod) is a human/CI step (see "Deploy handoff").
--
-- Idempotent: IF NOT EXISTS guards make a re-apply on a partially-migrated local
-- DB a no-op (the chain test re-runs the whole chain into a fresh DB each session).

BEGIN;

ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_attempts           INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS learner_update_failed_at          TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_last_error         TEXT        NULL,
    ADD COLUMN IF NOT EXISTS learner_update_next_attempt_at    TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_failed_permanently BOOLEAN     NOT NULL DEFAULT false;

-- Cheap drain scan: a PARTIAL index over only the pending, not-dead rows. The
-- predicate uses ONLY the two boolean columns — `next_attempt_at <= now()` is NOT
-- in the predicate (now() is non-immutable / not index-predicate-legal) and is
-- applied as a RUNTIME query filter by the janitor (WU-5B3a-1).
CREATE INDEX IF NOT EXISTS apollo_problem_attempts_pending_idx
    ON apollo_problem_attempts (created_at)
    WHERE learner_update_pending AND NOT learner_update_failed_permanently;

COMMIT;

-- Rollback (LOCAL ONLY — never run against a remote DB):
-- DROP INDEX IF EXISTS apollo_problem_attempts_pending_idx;
-- ALTER TABLE apollo_problem_attempts
--     DROP COLUMN IF EXISTS learner_update_attempts,
--     DROP COLUMN IF EXISTS learner_update_failed_at,
--     DROP COLUMN IF EXISTS learner_update_last_error,
--     DROP COLUMN IF EXISTS learner_update_next_attempt_at,
--     DROP COLUMN IF EXISTS learner_update_failed_permanently;
