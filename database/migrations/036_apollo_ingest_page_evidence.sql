-- 036_apollo_ingest_page_evidence.sql
-- WU-AAS ingestion observability (lane B2.4 / G4.4).
-- The authored-sets ingestion path never wrote apollo_ingest_runs /
-- apollo_ingest_errors (migration 030) and persisted NO per-page OCR text, so the
-- S2 ingestion audit ran on thin/absent inputs. This unit adds:
--   (a) apollo_ingest_runs.n_pages — the page count for a run (runs are now
--       written per authored-set ingestion, not only by the queue worker);
--   (b) apollo_ingest_page_evidence — one row per source page per run carrying the
--       recognized OCR text + self-reported confidence + extraction mode +
--       verify-path flag, the audit-facing projection of the transient OCR pass.
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 035_apollo_learner_state_space_idx.sql; 036 is
--     the next free number.
--   * Applied to LOCAL Docker Postgres ONLY by agents. Rehearsal on the TEST
--     Supabase project then prod is a human/CI step.
--
-- Guarded DDL (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS) so a re-apply
-- is a no-op, mirroring every migration in this tree.

BEGIN;

-- 1. apollo_ingest_runs: page count for a run (authored-set ingestion writes it).
ALTER TABLE apollo_ingest_runs
  ADD COLUMN IF NOT EXISTS n_pages INTEGER NOT NULL DEFAULT 0;

-- 1b. apollo_ingest_runs.document_id → NULLABLE. The authored-set path opens the
--     run BEFORE indexing so an OCR/indexing failure (bad PDF, no chunks produced)
--     still leaves a run row + error, instead of both observability tables staying
--     empty. At run-open no document has been minted yet, so the column must allow
--     NULL until problem indexing succeeds. The queue worker path always sets it.
--     Idempotent: DROP NOT NULL is a no-op if the column is already nullable.
ALTER TABLE apollo_ingest_runs
  ALTER COLUMN document_id DROP NOT NULL;

-- 2. apollo_ingest_page_evidence — per-page OCR text + confidence + verify flag.
--    document_id is NULLABLE: a page captured before the failure path minted a
--    document (a page-bearing-but-chunkless PDF) still gets its evidence persisted.
CREATE TABLE IF NOT EXISTS apollo_ingest_page_evidence (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  NOT NULL REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT,                    -- hidden authored doc (NULL if indexing failed pre-mint)
  role              TEXT    NOT NULL,          -- 'problem' | 'solution' (open enum)
  page_number       INTEGER,                   -- source page (NULL if the page carried none)
  ocr_text          TEXT    NOT NULL DEFAULT '',  -- recognized text/LaTeX for the page
  ocr_confidence    REAL,                      -- self-reported OCR confidence [0,1] (NULL if native)
  extraction_mode   TEXT,                      -- 'native' | 'ocr' | vendor-specific
  verify_path_fired BOOLEAN NOT NULL DEFAULT false,  -- page tripped the low-confidence threshold
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_page_evidence_run_idx
  ON apollo_ingest_page_evidence(ingest_run_id);
CREATE INDEX IF NOT EXISTS apollo_ingest_page_evidence_doc_idx
  ON apollo_ingest_page_evidence(document_id);

-- 3. RLS stopgap (mirror migration 030 §7): default-deny to PostgREST.
ALTER TABLE apollo_ingest_page_evidence ENABLE ROW LEVEL SECURITY;

COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; DROP TABLE IF EXISTS apollo_ingest_page_evidence;
-- ALTER TABLE apollo_ingest_runs DROP COLUMN IF EXISTS n_pages;
-- -- Re-tighten document_id ONLY if no run row has a NULL document_id:
-- -- ALTER TABLE apollo_ingest_runs ALTER COLUMN document_id SET NOT NULL;
-- COMMIT;
