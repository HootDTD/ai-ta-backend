-- Interaction 1: cache a student-safe retrieval bundle on Apollo tutoring sessions.
-- Nullable preserves the pre-feature behavior when the flag is off, retrieval is
-- empty, or the best-effort retrieval/persistence path fails.

ALTER TABLE app.learning_activities
    ADD COLUMN IF NOT EXISTS grounding_bundle JSONB;
