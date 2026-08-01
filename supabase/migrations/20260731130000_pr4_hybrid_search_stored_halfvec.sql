/*
 * PR-4: kill the per-row halfvec cast on the hybrid-search semantic arm.
 *
 * CONTEXT
 * -------
 * internal.document_chunks__embedding_halfvec_hnsw__idx (20260717035041) is
 * an EXPRESSION index on ((embedding)::extensions.halfvec(3072)). Matching
 * that expression in a query's ORDER BY is necessary but not sufficient for
 * the planner to use it: under the DB-08d RLS filter shape (chunk-local
 * `document_id = ANY(:ids)` over a materialized array, see
 * retrieval/hybrid_search.py::_build_semantic_cte), the planner consistently
 * chose a Seq Scan with a per-row cast instead. Staging
 * pg_stat_user_indexes shows 0 scans of that index under real traffic, while
 * the semantic CTE averages 630 ms (max 2.65 s in prod logs) on a ~1k-row
 * corpus.
 *
 * FIX
 * ---
 * Materialize the cast as a STORED generated column and index THAT column
 * directly -- no expression, no per-row cast at query time. Postgres
 * computes the halfvec once, at write time, and the HNSW index is then a
 * plain column index, which the planner treats very differently from an
 * expression index under a correlated/derived filter. This migration adds
 * `internal.document_chunks.embedding_halfvec` (`database/models.py`'s
 * `DocumentChunk.embedding_halfvec`, a SQLAlchemy `Computed()` column -- the
 * ORM never writes it) and its HNSW index. retrieval/hybrid_search.py's
 * semantic CTE is rewritten in the same PR to
 * `ORDER BY embedding_halfvec <=> query_halfvec` instead of casting
 * `embedding` per row (see `_halfvec_cosine_distance`).
 *
 * OLD EXPRESSION INDEX -- DROPPED
 * --------------------------------
 * Confirmed unused in real traffic (0 scans, see above) by every caller that
 * matters: the live Python retrieval path (repointed at the new column by
 * this PR) and the only other two SQL expressions that reference it,
 * `internal.hybrid_search()` / `internal.fetch_items()` (20260717050000).
 * Those two are legacy service_role-only SQL RPCs with no caller anywhere in
 * this repo (no `.rpc(`/PostgREST call site) -- dead code, not the live
 * path. TRADEOFF: if either is ever invoked directly, its ORDER BY still
 * casts `chunks.embedding::extensions.halfvec(3072)` per row, so it falls
 * back to the same brute-force-scan cost profile this migration fixes for
 * the live path, with no expression index left to (not) match. Repointing
 * those legacy functions at `embedding_halfvec` is out of scope here since
 * they are not on the request path; do it first if either is ever revived.
 *
 * TABLE REWRITE
 * -------------
 * Adding a STORED generated column rewrites the whole table, and the new
 * CREATE INDEX takes a write lock on internal.document_chunks for its build
 * duration -- the same tradeoff 023_chunks_halfvec_hnsw.sql (legacy) already
 * accepted for the original expression index. Indexing is batch/offline in
 * this system, so that is acceptable. If this is ever run against a busy
 * database, do the column add and an index build outside this transaction
 * (CREATE INDEX CONCURRENTLY) as separate manual steps instead.
 *
 * RE-RUNNABLE: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / DROP
 * INDEX IF EXISTS. A second apply is a no-op.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';

BEGIN;

DO $preconditions$
BEGIN
    IF to_regclass('internal.document_chunks') IS NULL THEN
        RAISE EXCEPTION 'PR-4 precondition failed: internal.document_chunks is missing';
    END IF;
    IF to_regtype('extensions.halfvec') IS NULL THEN
        RAISE EXCEPTION 'PR-4 precondition failed: extensions.halfvec type is missing (pgvector < 0.7?)';
    END IF;
END
$preconditions$;

ALTER TABLE internal.document_chunks
    ADD COLUMN IF NOT EXISTS embedding_halfvec extensions.halfvec(3072)
        GENERATED ALWAYS AS ((embedding)::extensions.halfvec(3072)) STORED;

-- Unused in real traffic (see header) -- superseded by the stored-column
-- index below, which indexes the same values without a per-row cast.
DROP INDEX IF EXISTS internal.document_chunks__embedding_halfvec_hnsw__idx;

CREATE INDEX IF NOT EXISTS document_chunks__embedding_halfvec_stored_hnsw__idx
    ON internal.document_chunks
    USING hnsw (embedding_halfvec extensions.halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ANALYZE internal.document_chunks;

COMMIT;
