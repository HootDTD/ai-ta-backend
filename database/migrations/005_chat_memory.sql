-- 005_chat_memory.sql
-- Chat ownership + memory summary keyed by chat_id and user_id.

BEGIN;

-- If an existing table already exists in Supabase, this migration is intended
-- for fresh installs. Existing deployments should run a manual data migration.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id               BIGSERIAL PRIMARY KEY,
    chat_id          TEXT    NOT NULL UNIQUE,
    user_id          UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id  INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    meta             JSONB   NOT NULL DEFAULT '{}'::jsonb,
    memory_summary   TEXT    NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_turns (
    id               BIGSERIAL PRIMARY KEY,
    chat_session_id  BIGINT  NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    turn_index       INTEGER NOT NULL,
    turn_id          TEXT    NOT NULL,
    role             TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content          TEXT    NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model            TEXT,
    tool_name        TEXT,
    tool_inputs      JSONB,
    attachments      JSONB   NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_chat_turns_session_turn_index
    ON chat_turns (chat_session_id, turn_index);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
    ON chat_sessions (user_id, updated_at DESC);

ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_turns ENABLE ROW LEVEL SECURITY;

COMMIT;
