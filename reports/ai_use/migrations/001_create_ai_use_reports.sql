-- SQL migration to create ai_use_reports table
CREATE TABLE IF NOT EXISTS ai_use_reports (
  id VARCHAR(36) PRIMARY KEY,
  chat_id VARCHAR(128) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  style VARCHAR(32),
  length VARCHAR(32),
  markdown TEXT,
  jsonld TEXT,
  model_fingerprint VARCHAR(128),
  tool_calls TEXT,
  prompt_hashes TEXT
);

-- Optional index on chat_id for retrieval by chat
CREATE INDEX IF NOT EXISTS idx_ai_use_reports_chat_id ON ai_use_reports(chat_id);

