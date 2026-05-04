-- 015_rag_orchestrator.sql
-- Adds RAG orchestrator persistence: cached snippets per chat session,
-- topic centroid for follow-up detection, and router decision telemetry.

BEGIN;

ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS topic_centroid_vector vector(3072) NULL;

CREATE TABLE IF NOT EXISTS chat_session_snippets (
  chat_session_id   BIGINT      NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  chunk_id          BIGINT      NOT NULL REFERENCES aita_chunks(id)   ON DELETE CASCADE,
  original_score    REAL        NOT NULL,
  first_seen_turn   INTEGER     NOT NULL,
  last_used_turn    INTEGER     NOT NULL,
  snippet_payload   JSONB       NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (chat_session_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_css_session_lru
  ON chat_session_snippets (chat_session_id, last_used_turn);

ALTER TABLE chat_session_snippets ENABLE ROW LEVEL SECURITY;
CREATE POLICY css_owner_only ON chat_session_snippets
  USING (
    chat_session_id IN (
      SELECT id FROM chat_sessions WHERE user_id = auth.uid()
    )
  );

CREATE TABLE IF NOT EXISTS chat_router_decisions (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  turn_id               TEXT       NOT NULL,
  chat_session_id       BIGINT     NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  query                 TEXT       NOT NULL,
  stage1_top1           REAL,
  stage1_margin         REAL,
  stage1_route          TEXT,
  stage2_invoked        BOOLEAN    NOT NULL DEFAULT FALSE,
  stage2_route          TEXT,
  stage2_confidence     REAL,
  retrieval_mode        TEXT       NOT NULL,
  retrieval_top_score   REAL,
  final_route           TEXT       NOT NULL,
  was_clarified         BOOLEAN    NOT NULL DEFAULT FALSE,
  clarify_cause         TEXT,
  latency_router_ms     INTEGER,
  latency_retrieval_ms  INTEGER,
  latency_answer_ms     INTEGER,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_router_decisions_session_ts
  ON chat_router_decisions (chat_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_router_decisions_route_ts
  ON chat_router_decisions (final_route, created_at DESC);

ALTER TABLE chat_router_decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY crd_owner_only ON chat_router_decisions
  USING (
    chat_session_id IN (
      SELECT id FROM chat_sessions WHERE user_id = auth.uid()
    )
  );

COMMIT;
