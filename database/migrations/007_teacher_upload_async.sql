-- 007_teacher_upload_async.sql
-- Expand teacher uploads for async ingestion and add durable worker jobs.

BEGIN;

ALTER TABLE teacher_uploads
    ADD COLUMN IF NOT EXISTS status TEXT,
    ADD COLUMN IF NOT EXISTS storage_key TEXT,
    ADD COLUMN IF NOT EXISTS artifact_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS ocr_provider TEXT,
    ADD COLUMN IF NOT EXISTS ocr_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS warning_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS error_message TEXT,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

UPDATE teacher_uploads
SET status = CASE
    WHEN COALESCE(is_latest, FALSE) THEN 'ready'
    ELSE 'superseded'
END
WHERE status IS NULL;

ALTER TABLE teacher_uploads
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'ready';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'teacher_uploads_status_check'
    ) THEN
        ALTER TABLE teacher_uploads
            ADD CONSTRAINT teacher_uploads_status_check
            CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'superseded'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_teacher_uploads_status
    ON teacher_uploads (status, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_teacher_uploads_storage_key
    ON teacher_uploads (storage_key);

CREATE TABLE IF NOT EXISTS teacher_upload_jobs (
    id               BIGSERIAL PRIMARY KEY,
    upload_id        BIGINT NOT NULL
        REFERENCES teacher_uploads(id) ON DELETE CASCADE,
    state            TEXT NOT NULL DEFAULT 'queued',
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'teacher_upload_jobs_state_check'
    ) THEN
        ALTER TABLE teacher_upload_jobs
            ADD CONSTRAINT teacher_upload_jobs_state_check
            CHECK (state IN ('queued', 'processing', 'completed', 'failed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_teacher_upload_jobs_queue
    ON teacher_upload_jobs (state, lease_expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_teacher_upload_jobs_upload_id
    ON teacher_upload_jobs (upload_id);

COMMIT;
