-- 022_chunks_halfvec_hnsw.sql
-- RetrievalV2: make the chunk-level vector index reproducible from migrations.
--
-- 001_create_schema.py skips HNSW entirely for EMBEDDING_DIM > 2000 (it
-- predates halfvec). The production index idx_aita_chunks_embedding_hnsw was
-- created by hand; this migration codifies it so fresh environments match.
--
-- Storage strategy (same as 019_apollo_misconceptions.sql):
--   - Column stays vector(3072) (full float32 precision).
--   - HNSW expression index casts to halfvec(3072) at index time
--     (HNSW limit is 4000 dims for halfvec, 2000 for vector). 50% less
--     memory; precision loss negligible for cosine on text embeddings.
--   - Queries MUST cast BOTH operands to halfvec(3072) to match this
--     expression (see retrieval/hybrid_search.py::_halfvec_cosine_distance).
--
-- NOTE: CREATE INDEX takes a write lock on aita_chunks for the build
-- duration. Indexing is batch/offline in this system, so that is acceptable.
-- If applying to a busy production DB, run the CREATE INDEX CONCURRENTLY
-- variant outside a transaction instead.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_aita_chunks_embedding_hnsw
    ON aita_chunks
    USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ANALYZE aita_chunks;

COMMIT;
