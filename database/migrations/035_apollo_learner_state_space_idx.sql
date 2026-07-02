-- 035_apollo_learner_state_space_idx.sql
-- Campaign-plan Task B3: the classroom mastery-heatmap projection scans every
-- apollo_learner_state row for a course (search_space_id), then joins to
-- apollo_kg_entities for the concept grouping. apollo_learner_state's PRIMARY
-- KEY is (user_id, search_space_id, entity_id) -- user_id leads, so a
-- course-wide scan cannot use the PK index. Mirrors migration 034's
-- ix_grading_artifacts_space_concept_time shape for apollo_grading_artifacts.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_apollo_learner_state_space_entity
    ON apollo_learner_state (search_space_id, entity_id);

COMMIT;
