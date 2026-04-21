-- 012_ai_use_reports.sql
-- Create the ai_use_reports table used by reports/ai_use/models.py. The
-- table was previously created by hand in the old Supabase project and was
-- never backed by a migration; provisioning a fresh Supabase project left
-- /reports/ai-use endpoints 4xx/5xx until now.
--
-- Ownership is enforced at the application layer (see
-- reports/ai_use/routes.py::_require_owned_report). The backend uses the
-- Supabase anon key to talk to PostgREST here, so we leave RLS disabled on
-- this table rather than synthesising auth.uid() context from a service
-- role. All reads/writes funnel through the FastAPI routes which already
-- check chat ownership.

BEGIN;

CREATE TABLE IF NOT EXISTS ai_use_reports (
    id                 UUID        PRIMARY KEY,
    chat_id            TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    style              TEXT,
    length             TEXT,
    markdown           TEXT,
    jsonld             JSONB,
    model_fingerprint  TEXT,
    tool_calls         JSONB,
    prompt_hashes      JSONB
);

CREATE INDEX IF NOT EXISTS idx_ai_use_reports_chat_created
    ON ai_use_reports (chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_use_reports_created
    ON ai_use_reports (created_at DESC);

COMMIT;
