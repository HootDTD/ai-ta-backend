-- 031_apollo_soundness_applicable.sql
-- D5/D6 — soundness N/A on an empty misconception bank.
-- Design: docs/design/2026-06-23-apollo-soundness-na-sentinel.md
--
-- Adds a single authoritative flag distinguishing a VERIFIED-sound score from a
-- never-checked one (the bank was empty/absent for the concept). When false:
--   * soundness_score / bisimilarity_score hold the COVERAGE-ONLY fallback value
--     (NOT NULL kept; the harmonic mean renormalizes to coverage, never 0.0/1.0);
--   * contradiction_score is NULL (already nullable; no change needed).
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 030_apollo_autoprovisioning.sql; 031 is next free.
--   * Applied to LOCAL Docker Postgres ONLY by agents. Rehearsal on the TEST
--     Supabase project then prod is a human/CI step.
--
-- ROLLBACK: dropping soundness_applicable loses the verified-vs-N/A distinction;
-- the numeric columns are unaffected (they always held an in-range scalar). Safe
-- direction: after rollback every row reads as if applicable (pre-031 behavior).

BEGIN;

ALTER TABLE apollo_graph_comparison_runs
    ADD COLUMN IF NOT EXISTS soundness_applicable BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN apollo_graph_comparison_runs.soundness_applicable IS
    'D5/D6: false iff the misconception bank was empty/absent for the concept; '
    'then soundness_score/bisimilarity_score are the coverage-only fallback and '
    'contradiction_score is NULL. Readers must check this before trusting soundness.';

COMMIT;
