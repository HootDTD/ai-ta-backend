-- INTERACTION1: nullable per-session grounding-bundle cache on Apollo
-- tutoring sessions. Mirrors database/migrations/048 (legacy chain); a NULL
-- column preserves pre-feature behavior when the flag is off, retrieval is
-- empty, or the best-effort write fails.

ALTER TABLE app.learning_activities
    ADD COLUMN IF NOT EXISTS grounding_bundle JSONB;
