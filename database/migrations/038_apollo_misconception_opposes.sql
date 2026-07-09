-- 038_apollo_misconception_opposes.sql
-- F-struct (structural co-key): add the per-entry `opposes` link to the
-- RUNTIME misconception bank (apollo_misconceptions, migration 019). The link
-- already exists in the on-disk misconceptions.json source and in the emergent
-- store (apollo_misconception_observations.opposes, migration 037) + the
-- apollo_kg_entities opposes_entity_key payload — only the detector's bank
-- lacked it. Nullable: most banks have no opposes; NULL means "no structural
-- scope" and the structural co-key gate path never fires for that entry.
--
-- Numbering: on-disk max was 037; this takes 038.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

ALTER TABLE apollo_misconceptions
    ADD COLUMN IF NOT EXISTS opposes TEXT;

COMMENT ON COLUMN apollo_misconceptions.opposes IS
    'F-struct: canonical entity_key of the reference node this misconception '
    'contradicts (e.g. def.real_basis), or NULL. Seeded from misconceptions.json '
    '"opposes". Read by the structural co-key gate path to NAME a misconception '
    'the judge only localized.';

COMMIT;
