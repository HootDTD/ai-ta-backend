/*
 * DB-06: pin pgvector to extensions and harden the target retrieval RPCs.
 *
 * This migration runs only after DB-04 has built the target schemas and DB-05
 * has copied legacy data. It does not mutate legacy public tables.
 *
 * HUMAN-ONLY LIVE RELOCATION GATE (never run blindly or by automation):
 *
 *   -- 1. Rehearse on an exact production snapshot, then on the test project.
 *   CREATE SCHEMA IF NOT EXISTS extensions;
 *   ALTER EXTENSION vector SET SCHEMA extensions;
 *
 *   -- 2. Verify the namespace, every pgvector index, and a forced ANN plan.
 *   SELECT n.nspname
 *   FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
 *   WHERE e.extname = 'vector';
 *   SELECT indexrelid::regclass, indisvalid, pg_get_indexdef(indexrelid)
 *   FROM pg_index
 *   WHERE indexrelid IN (
 *     'public.idx_aita_chunks_embedding_hnsw'::regclass,
 *     'internal.document_chunks__embedding_halfvec_hnsw__idx'::regclass
 *   );
 *   -- Run EXPLAIN with enable_seqscan=off for the halfvec cosine ORDER BY,
 *   -- then compare test-project recall and latency with the pre-move baseline.
 *
 *   -- 3. ABORT the extension move independently if vector is not relocatable,
 *   -- either HNSW index is invalid/unused, or recall/latency regresses. Leave
 *   -- vector in public temporarily; do not rebuild a production ANN index in
 *   -- the cutover window. The broader target cutover may proceed separately.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';

BEGIN;

CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

DO $preconditions$
DECLARE
    extension_schema text;
BEGIN
    SELECT namespace.nspname
    INTO extension_schema
    FROM pg_extension AS extension
    JOIN pg_namespace AS namespace ON namespace.oid = extension.extnamespace
    WHERE extension.extname = 'vector';

    IF extension_schema IS DISTINCT FROM 'extensions' THEN
        RAISE EXCEPTION
            'DB-06 precondition failed: vector is in schema %, expected extensions; run the human-observed relocation gate',
            extension_schema;
    END IF;
    IF to_regclass('internal.document_chunks') IS NULL THEN
        RAISE EXCEPTION 'DB-06 precondition failed: internal.document_chunks is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        RAISE EXCEPTION 'DB-06 precondition failed: calling role service_role is missing';
    END IF;
END
$preconditions$;

-- DB-04 already owns all three section 6.2 retrieval indexes. Reuse them;
-- creating another spelling of any access path would violate the allowlist.
DO $retrieval_indexes$
DECLARE
    index_name text;
BEGIN
    FOREACH index_name IN ARRAY ARRAY[
        'document_chunks__document_page__idx',
        'document_chunks__content_fts__idx',
        'document_chunks__embedding_halfvec_hnsw__idx'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_index AS index_meta
            JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
            JOIN pg_namespace AS namespace ON namespace.oid = index_class.relnamespace
            WHERE namespace.nspname = 'internal'
              AND index_class.relname = index_name
              AND index_meta.indisvalid
        ) THEN
            RAISE EXCEPTION 'DB-06 precondition failed: required valid index internal.% is missing', index_name;
        END IF;
    END LOOP;
END
$retrieval_indexes$;

-- Remove DB-04's provisional bigint overloads before installing the reviewed
-- live contracts. Keeping both would make PostgREST integer-array calls
-- ambiguous and would silently preserve a non-contract return shape.
DROP FUNCTION internal.fetch_items(bigint[], bigint[]);
DROP FUNCTION internal.fts_count(text, bigint[]);
DROP FUNCTION internal.hybrid_search(
    text, extensions.vector, bigint[], integer, integer
);

CREATE FUNCTION internal.fetch_items(
    p_chunk_ids integer[],
    p_document_ids integer[]
)
RETURNS TABLE (
    id integer,
    content text,
    embedding extensions.vector,
    page_number integer,
    section_path text,
    chunk_type character varying(20),
    figure_id character varying,
    document_id integer,
    created_at timestamptz
)
LANGUAGE sql
STABLE
SET search_path = ''
AS $function$
    SELECT
        chunks.id::integer,
        chunks.content,
        chunks.embedding,
        chunks.page_number,
        chunks.section_path,
        chunks.chunk_type::character varying(20),
        chunks.figure_id::character varying,
        chunks.document_id::integer,
        chunks.created_at
    FROM internal.document_chunks AS chunks
    WHERE chunks.id = ANY (p_chunk_ids)
      AND chunks.document_id = ANY (p_document_ids)
$function$;

CREATE FUNCTION internal.fts_count(
    query_text text,
    p_document_ids integer[]
)
RETURNS integer
LANGUAGE sql
STABLE
SET search_path = ''
AS $function$
    SELECT count(*)::integer
    FROM internal.document_chunks AS chunks
    WHERE chunks.document_id = ANY (p_document_ids)
      AND pg_catalog.to_tsvector('english'::pg_catalog.regconfig, chunks.content)
          OPERATOR(pg_catalog.@@)
          pg_catalog.websearch_to_tsquery('english'::pg_catalog.regconfig, query_text)
$function$;

CREATE FUNCTION internal.hybrid_search(
    query_text text,
    query_embedding extensions.vector,
    p_document_ids integer[],
    match_count integer,
    rrf_k integer DEFAULT 60
)
RETURNS TABLE (
    chunk_id integer,
    document_id integer,
    semantic_score double precision,
    lexical_score double precision,
    semantic_rank integer,
    lexical_rank integer
)
LANGUAGE sql
STABLE
SET search_path = ''
AS $function$
    WITH sem AS (
        SELECT
            chunks.id,
            chunks.document_id,
            1.0 - (
                chunks.embedding::extensions.halfvec(3072)
                OPERATOR(extensions.<=>)
                query_embedding::extensions.halfvec(3072)
            ) AS score,
            pg_catalog.row_number() OVER (
                ORDER BY
                    chunks.embedding::extensions.halfvec(3072)
                    OPERATOR(extensions.<=>)
                    query_embedding::extensions.halfvec(3072)
            )::integer AS rnk
        FROM internal.document_chunks AS chunks
        ORDER BY
            chunks.embedding::extensions.halfvec(3072)
            OPERATOR(extensions.<=>)
            query_embedding::extensions.halfvec(3072)
        LIMIT match_count * 10
    ),
    sem_filtered AS (
        SELECT *
        FROM sem
        WHERE document_id = ANY (p_document_ids)
        LIMIT match_count * 2
    ),
    lex AS (
        SELECT
            chunks.id,
            chunks.document_id,
            pg_catalog.ts_rank_cd(
                pg_catalog.to_tsvector('english'::pg_catalog.regconfig, chunks.content),
                pg_catalog.websearch_to_tsquery('english'::pg_catalog.regconfig, query_text)
            ) AS score,
            pg_catalog.row_number() OVER (
                ORDER BY pg_catalog.ts_rank_cd(
                    pg_catalog.to_tsvector('english'::pg_catalog.regconfig, chunks.content),
                    pg_catalog.websearch_to_tsquery('english'::pg_catalog.regconfig, query_text)
                ) DESC
            )::integer AS rnk
        FROM internal.document_chunks AS chunks
        WHERE chunks.document_id = ANY (p_document_ids)
          AND pg_catalog.to_tsvector('english'::pg_catalog.regconfig, chunks.content)
              OPERATOR(pg_catalog.@@)
              pg_catalog.websearch_to_tsquery('english'::pg_catalog.regconfig, query_text)
        LIMIT match_count * 2
    )
    SELECT
        coalesce(sem_filtered.id, lex.id)::integer,
        coalesce(sem_filtered.document_id, lex.document_id)::integer,
        coalesce(sem_filtered.score, 0.0)::double precision,
        coalesce(lex.score, 0.0)::double precision,
        coalesce(sem_filtered.rnk, match_count * 2 + 1)::integer,
        coalesce(lex.rnk, match_count * 2 + 1)::integer
    FROM sem_filtered
    FULL OUTER JOIN lex
      ON sem_filtered.id = lex.id
     AND sem_filtered.document_id = lex.document_id
    ORDER BY (
        1.0 / (rrf_k + coalesce(sem_filtered.rnk, match_count * 2 + 1))
        + 1.0 / (rrf_k + coalesce(lex.rnk, match_count * 2 + 1))
    ) DESC
    LIMIT match_count
$function$;

REVOKE ALL ON FUNCTION internal.fetch_items(integer[], integer[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION internal.fts_count(text, integer[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION internal.hybrid_search(
    text, extensions.vector, integer[], integer, integer
) FROM PUBLIC;

GRANT USAGE ON SCHEMA internal, extensions TO service_role;
GRANT SELECT ON internal.document_chunks TO service_role;
GRANT EXECUTE ON FUNCTION internal.fetch_items(integer[], integer[]) TO service_role;
GRANT EXECUTE ON FUNCTION internal.fts_count(text, integer[]) TO service_role;
GRANT EXECUTE ON FUNCTION internal.hybrid_search(
    text, extensions.vector, integer[], integer, integer
) TO service_role;

COMMIT;
