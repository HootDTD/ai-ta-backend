-- 011_chat_turn_citations.sql
-- Persist citation badges per assistant turn so old chats can re-render them.
-- Prior to this migration, citations were computed at request time, returned
-- to the client in the /ask response, and then discarded — reloading a chat
-- lost every badge.

BEGIN;

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS citations JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
