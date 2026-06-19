-- 030_apollo_autoprovisioning.sql
-- §8B materials -> Apollo auto-provisioning substrate (WU-3B2a).
-- Spec: docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md §8B.
-- Adjudicated DDL: docs/superpowers/plans/2026-06-19-apollo-kg-wu3b2-split-proposal.md
--   '### WU-3B2a' + 'ORCHESTRATOR ADJUDICATION (2026-06-19)' decisions #3/#6/#11.
--
-- This unit ships SCHEMA + ORM + the Tier-2 selection gate ONLY. No pipeline/LLM logic.
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 029_chat_turn_keywords.sql; 030 is the next free number.
--   * This file is applied to LOCAL Docker Postgres ONLY by feller agents. Rehearsal on the
--     TEST Supabase project then prod is a human/CI step (see plan "Deploy handoff").
--
-- TWO BACKFILLS (both inside this migration):
--   (a) denormalize search_space_id onto apollo_concept_problems via the concept->subject join.
--   (b) CRITICAL — flip every existing (seeded) tier=1 row to tier=2/'authored' so the live
--       bernoulli pool stays teachable the instant the tier filter ships. Highest blast radius.
--
-- ROLLBACK CAVEAT (see foot of file): dropping `tier` loses the tier=1/tier=2 distinction;
-- backfill (b) is NOT separately reversible — after rollback every problem is teachable again
-- (the pre-030 behavior), which is the safe direction. The search_space_id denormalization is a
-- copy and is dropped cleanly (the authoritative scope is still reachable via concept->subject).

BEGIN;
-- 1. apollo_concept_problems: tier/solution_source/provenance/quarantine + denormalized scope.
ALTER TABLE apollo_concept_problems
  ADD COLUMN IF NOT EXISTS tier            SMALLINT NOT NULL DEFAULT 1
      CHECK (tier IN (1, 2)),                              -- 1=inventory (not teachable), 2=teachable
  ADD COLUMN IF NOT EXISTS solution_source TEXT
      CHECK (solution_source IS NULL OR solution_source IN ('extracted','generated','authored')),
  ADD COLUMN IF NOT EXISTS provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {document_id,page,chunk_content_hash}
  ADD COLUMN IF NOT EXISTS quarantined_at  TIMESTAMPTZ,                          -- §8B.3c anomaly quarantine (NULL = live); FILTER added in WU-3B2h, NOT here
  ADD COLUMN IF NOT EXISTS search_space_id INTEGER;                             -- denormalized course scope (ADJUDICATION #3: NULLABLE here)
-- backfill (a): denormalize the course scope from the concept->subject chain.
UPDATE apollo_concept_problems p SET search_space_id = sub.search_space_id
  FROM apollo_concepts c JOIN apollo_subjects sub ON sub.id = c.subject_id
  WHERE p.concept_id = c.id AND p.search_space_id IS NULL;
-- backfill (b) CRITICAL: existing §8-seeded rows ARE teachable -> tier=2, solution_source='authored'.
UPDATE apollo_concept_problems SET tier = 2, solution_source = 'authored' WHERE tier = 1;
CREATE INDEX IF NOT EXISTS apollo_concept_problems_concept_tier_idx
  ON apollo_concept_problems(concept_id, tier);

-- 1b. apollo_kg_entities: scope_summary text (the dedup embedding SOURCE, embedded on the fly; 3B2c).
ALTER TABLE apollo_kg_entities
  ADD COLUMN IF NOT EXISTS scope_summary TEXT;  -- nullable; authored at mint; NO persisted vector (no pgvector migration)

-- 2. apollo_ingest_runs — per-doc counts + LLM call/token/cost aggregates + content_hash + status.
CREATE TABLE IF NOT EXISTS apollo_ingest_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  content_hash      TEXT,                 -- document content hash; an unchanged re-upload short-circuits (3B2g)
  status            TEXT    NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
  n_questions_scraped INTEGER NOT NULL DEFAULT 0,
  n_promoted        INTEGER NOT NULL DEFAULT 0,
  n_rejected        INTEGER NOT NULL DEFAULT 0,
  n_dedup_merged    INTEGER NOT NULL DEFAULT 0,
  llm_calls         INTEGER NOT NULL DEFAULT 0,
  llm_tokens_in     BIGINT  NOT NULL DEFAULT 0,
  llm_tokens_out    BIGINT  NOT NULL DEFAULT 0,
  llm_cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_space_doc_idx ON apollo_ingest_runs(search_space_id, document_id);
-- a succeeded run per (document_id, content_hash) lets enqueue short-circuit an unchanged re-upload:
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_doc_hash_idx ON apollo_ingest_runs(document_id, content_hash);

-- 3. apollo_provisioning_jobs — the SKIP LOCKED work queue (mirrors TeacherUploadJob lease shape).
CREATE TABLE IF NOT EXISTS apollo_provisioning_jobs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  state             TEXT    NOT NULL DEFAULT 'pending'
      CHECK (state IN ('pending','running','completed','failed')),
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE SET NULL,
  lease_owner       TEXT,
  lease_expires_at  TIMESTAMPTZ,
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- idempotent enqueue: at most one OPEN job per document (partial-unique-index idiom).
-- JOB-level dedup; the (document_id, chunk_content_hash) no-op is a SEPARATE intra-job guarantee (3B2g).
CREATE UNIQUE INDEX IF NOT EXISTS apollo_provisioning_jobs_open_uniq
  ON apollo_provisioning_jobs(document_id) WHERE state IN ('pending','running');

-- 4. apollo_rejected_problems — a gate failure + diagnostic + the rejected payload.
CREATE TABLE IF NOT EXISTS apollo_rejected_problems (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,  -- NULL if rejected pre-tag
  failed_gate       SMALLINT,            -- 1..8 (NULL if rejected at the pairing gate, not a lint gate)
  rejected_stage    TEXT    NOT NULL,    -- 'pairing_gate' | 'promotion_lint'
  diagnostic        TEXT    NOT NULL,
  payload           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_rejected_problems_run_idx ON apollo_rejected_problems(ingest_run_id);

-- 5. apollo_dedup_decisions — method + similarity + verdict for every dedup resolution.
CREATE TABLE IF NOT EXISTS apollo_dedup_decisions (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,
  candidate_key     TEXT    NOT NULL,
  method            TEXT    NOT NULL CHECK (method IN ('slug','embedding','llm_judge')),
  similarity        REAL,                -- NULL for the slug exact-match tier
  verdict           TEXT    NOT NULL CHECK (verdict IN ('merged','distinct')),
  matched_entity_id BIGINT  REFERENCES apollo_kg_entities(id) ON DELETE SET NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_dedup_decisions_run_idx ON apollo_dedup_decisions(ingest_run_id);

-- 6. apollo_ingest_errors — stage + class + context for any non-terminal pipeline error.
CREATE TABLE IF NOT EXISTS apollo_ingest_errors (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  stage             TEXT    NOT NULL,    -- scrape|find_or_generate|pairing|tag_mint|dedup|promotion
  error_class       TEXT    NOT NULL,
  context           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_errors_run_idx ON apollo_ingest_errors(ingest_run_id);

-- 7. RLS stopgap (mirror 026 §7 / migration 022): default-deny to PostgREST on the 5 new tables.
ALTER TABLE apollo_ingest_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_provisioning_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_rejected_problems  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_dedup_decisions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_ingest_errors      ENABLE ROW LEVEL SECURITY;
COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; DROP TABLE IF EXISTS apollo_ingest_errors, apollo_dedup_decisions, apollo_rejected_problems,
--   apollo_provisioning_jobs, apollo_ingest_runs;
-- ALTER TABLE apollo_kg_entities DROP COLUMN IF EXISTS scope_summary;
-- DROP INDEX IF EXISTS apollo_concept_problems_concept_tier_idx;
-- ALTER TABLE apollo_concept_problems DROP COLUMN IF EXISTS search_space_id, DROP COLUMN IF EXISTS quarantined_at,
--   DROP COLUMN IF EXISTS provenance, DROP COLUMN IF EXISTS solution_source, DROP COLUMN IF EXISTS tier; COMMIT;
