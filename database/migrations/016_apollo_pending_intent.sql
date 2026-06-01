-- 016_apollo_pending_intent.sql
-- Item #5: add `pending_intent` column to apollo_sessions so the chat
-- handler can stash a one-turn confirmation ("you sounded like you were
-- done teaching — confirm?") and resolve it on the next student turn.
--
-- Nullable. Cleared when the next turn arrives (whether the student
-- confirmed or not). Values are the string forms of the Intent literal
-- in `apollo/handlers/intent.py` — application-side enforced, not a
-- DB enum (additive intents shouldn't require migrations).

BEGIN;

ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS pending_intent TEXT;

COMMIT;
