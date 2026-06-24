-- 031_apollo_subject_profile.sql
-- Subject-fluid Apollo -> per-subject PROFILE on apollo_subjects.
-- Spec: docs/superpowers/specs/2026-06-23-subject-fluid-apollo-design.md §4.1 / §5 / §8.
--
-- The auto-detected, persisted subject profile drives which promotion-lint gates,
-- node vocabulary, target contract and validator apply to a subject. Two built-ins
-- ship in apollo.provisioning.subject_profile: 'quantitative_symbolic' (default,
-- back-compat — all 8 gates, symbol target) and 'qualitative_argumentative'
-- (gates 1/2/3/8 + faithfulness, prose target).
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 030_apollo_autoprovisioning.sql; 031 is next free.
--   * STAGING ONLY for this work (hjevtxdt…), never prod (uduxdnii…). The staging
--     migration-tracking table is known out-of-sync — trust schema INSPECTION, not
--     list_migrations, before/after applying.
--   * Apply is a human/CI step. This file is idempotent (ADD COLUMN IF NOT EXISTS).
--
-- BACK-COMPAT (the load-bearing line): backfill EVERY existing subject to
-- 'quantitative_symbolic' so the live fluid-mechanics curriculum keeps today's
-- all-8-gates behavior unchanged (the 41 seeded ss=2 :Canon still promote). The
-- column is NOT NULL with that server_default so any raw-SQL / ORM insert that
-- omits it also lands on the strict default.
--
-- ROLLBACK CAVEAT (see foot of file): dropping the three columns loses the
-- per-subject profile; after rollback every subject is implicitly
-- quantitative_symbolic again (the pre-031 behavior), which is the safe direction.

BEGIN;
ALTER TABLE apollo_subjects
  ADD COLUMN IF NOT EXISTS profile_kind       TEXT NOT NULL DEFAULT 'quantitative_symbolic',
  ADD COLUMN IF NOT EXISTS profile_confidence REAL,                                  -- probe confidence; NULL until a detection runs
  ADD COLUMN IF NOT EXISTS profile_evidence   JSONB NOT NULL DEFAULT '{}'::jsonb;    -- probe audit trail (n_problems, prose_fraction, …)
-- Backfill (defensive): any pre-existing row that somehow predates the DEFAULT
-- lands on the strict back-compat profile too.
UPDATE apollo_subjects SET profile_kind = 'quantitative_symbolic' WHERE profile_kind IS NULL;
COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; ALTER TABLE apollo_subjects
--   DROP COLUMN IF EXISTS profile_evidence,
--   DROP COLUMN IF EXISTS profile_confidence,
--   DROP COLUMN IF EXISTS profile_kind; COMMIT;
