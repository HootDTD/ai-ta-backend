-- 019_apollo_misconceptions.sql
-- Class 2 Phase 2: per-concept misconception bank backing the
-- misconception-inference channel (apollo/overseer/misconception.py).
--
-- The pipeline (per Macina 2024 + MISTAKE 2510.11502 + G-R-R 2602.02414):
--   1. LLM generates a candidate misconception from the student utterance.
--   2. Embed the candidate; nearest-neighbor over `description_embedding`
--      restricted to the session's concept_id finds attested matches.
--   3. Macina-style verifier confirms the match.
--   4. On verified hit, the row's probe_question / rt_steps drive Apollo's
--      Socratic-debugging persona shift.
--
-- Storage strategy per CLAUDE.md:
--   - Store as `vector(3072)` (full float32 precision).
--   - HNSW expression index casts to `halfvec(3072)` at index time
--     (HNSW limit is 4000 dims for halfvec, 2000 for vector). 50% less
--     memory; precision loss is negligible for cosine on text embeddings.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_misconceptions (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id            BIGINT NOT NULL
        REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    -- Author-facing stable identifier (e.g. 'no_density', 'pressure_for_velocity').
    -- Lets curriculum authors reference a misconception in tests + analytics
    -- without depending on the surrogate id.
    code                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    description_embedding vector(3072),
    -- Optional structured pair encoding the conceptual confusion
    -- (e.g. confusion_pair_a='mass_flow', confusion_pair_b='volumetric_flow').
    -- Used for analytics; the inference pipeline retrieves on the embedding,
    -- not the pair.
    confusion_pair_a      TEXT,
    confusion_pair_b      TEXT,
    -- Verbatim student phrasings that hint at this misconception. Used by
    -- the optional fast-path matcher and as authoring guidance.
    trigger_phrases       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Apollo-voiced probing question fed into the persona shift on a
    -- TAU_PROBE-band detection. Must read in confused-tutee voice.
    probe_question        TEXT NOT NULL,
    -- Reasoning Trajectory steps (per arXiv 2511.00371) — the sequence of
    -- diagnostic moves Apollo walks the student through on a TAU_FIRE-band
    -- detection. Shorter is better (paper finding: validity is inversely
    -- correlated with trajectory length).
    rt_steps              JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_id, code)
);

CREATE INDEX IF NOT EXISTS apollo_misconceptions_concept_idx
    ON apollo_misconceptions(concept_id);

-- HNSW expression index — halfvec, cosine distance.
-- See CLAUDE.md "Critical Stack Decision: HNSW Indexing Strategy for 3072
-- Dimensions" for the rationale.
CREATE INDEX IF NOT EXISTS apollo_misconceptions_embedding_hnsw_idx
    ON apollo_misconceptions
    USING hnsw ((description_embedding::halfvec(3072)) halfvec_cosine_ops);

COMMENT ON TABLE apollo_misconceptions IS
    'Per-concept authored misconception bank for the misconception-inference '
    'channel (Apollo Gap B). Curriculum authors INSERT rows here when a '
    'class is added; runtime queries by concept_id (set on apollo_sessions).';

COMMIT;
