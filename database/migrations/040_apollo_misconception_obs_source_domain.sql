-- 040_apollo_misconception_obs_source_domain.sql
-- Emergent misconception map (design 2026-07-10, §5.3/§5.4): enforce the
-- `source` domain on apollo_misconception_observations (migration 037).
-- `source` was plain TEXT DEFAULT 'grading_artifact' with no CHECK; this adds
-- one enumerating the three write-path values: the pre-existing
-- 'grading_artifact' (role=canonical grading-artifact ledger feed, unchanged)
-- plus the two new emergent-map capture seams' sources, 'detector_unkeyed'
-- (judge confidently wrong at a keyed reference node it cannot name — the
-- birth signal) and 'clarification_refuted' (a clarification rescore that
-- confirms the misconception — the upgrade signal). No behavior change to
-- any existing write: 'grading_artifact' is already the only value ever
-- written on staging/prod.
--
-- Numbering: on-disk max was 039 (039_apollo_misconception_opposes.sql); this
-- takes 040.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.
--
-- ROLLBACK: dropping the constraint (ck_misconception_obs_source) restores the
-- unconstrained TEXT column; no data is lost or rewritten either direction.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_misconception_obs_source'
    ) THEN
        ALTER TABLE apollo_misconception_observations
            ADD CONSTRAINT ck_misconception_obs_source
            CHECK (source IN ('grading_artifact', 'detector_unkeyed', 'clarification_refuted'));
    END IF;
END $$;

COMMENT ON COLUMN apollo_misconception_observations.source IS
    'Observation origin: grading_artifact (role=canonical Done-grade ledger '
    'feed, migration 037, unchanged) | detector_unkeyed (emergent-map birth '
    'signal — judge confidently wrong at a keyed reference node it cannot '
    'name) | clarification_refuted (emergent-map upgrade signal — a '
    'clarification rescore confirms the misconception). Enumerated by '
    'ck_misconception_obs_source (migration 040).';

COMMIT;
