-- 029_chat_turn_keywords.sql
-- §10 RQ5 hedge (spec docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-
--   architecture-decision.md:1453-1466; §12 phase-5 L1534).
-- Persist the per-/ask extract_and_filter_keywords output (<=8 concept terms)
-- as ONE write-only JSONB column on chat_turns. Until now these keywords were
-- computed at request time as retrieval hints and then DISCARDED. Persisting
-- them lets months of chat history be backfilled offline as a CLASS-LEVEL
-- signal (aggregate concept coverage across a course) -- never a hard
-- per-student negative, and with NO read/consumer path in v1 (write-only).
--
-- Safe / zero-downtime: ADD COLUMN with a constant server default is a
-- metadata-only catalog change on Postgres 11+ (no full-table rewrite). NOT NULL
-- is safe because a non-volatile DEFAULT is supplied in the same statement.
-- IF NOT EXISTS makes re-application a no-op (idempotent).

BEGIN;

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS keywords JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;

-- Rollback for 029_chat_turn_keywords.sql:
-- BEGIN;
-- ALTER TABLE chat_turns DROP COLUMN IF EXISTS keywords;
-- COMMIT;
