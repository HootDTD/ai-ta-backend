-- 017_apollo_history_summary.sql
-- Item #2: rolling history summary so Apollo's chat context stays bounded.
--
-- `history_summary` holds a short LLM-generated digest of conversation
-- turns older than the windowing cutoff. `history_summary_up_to_turn`
-- records the highest turn_index covered by that summary so we know
-- when to refresh it (every K new turns) without reading every message.

BEGIN;

ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS history_summary TEXT;

ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS history_summary_up_to_turn INTEGER;

COMMIT;
