-- 038: reversed-provisioning concept matching needs a human-readable
-- description per registered concept (the closed-list matcher prompt is
-- "slug — display_name: description"; protocol measured at 95-96.7% in
-- apollo/provisioning/corpora/calc2/authored/results_authored.md).
-- Additive; default '' keeps every existing row and seeder valid.
ALTER TABLE apollo_concepts
    ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
