-- DAG-1: persist the mint-time dedup-pressure gauge on each ingestion run.
-- Idempotent additive DDL; remote application remains a human/CI step.
-- Default is the empty object (mirrors the ORM server_default); readers
-- .get() every key, and ORM inserts populate the zeroed gauge shape.

ALTER TABLE apollo_ingest_runs
    ADD COLUMN IF NOT EXISTS dedup_pressure JSONB NOT NULL DEFAULT '{}'::jsonb;
