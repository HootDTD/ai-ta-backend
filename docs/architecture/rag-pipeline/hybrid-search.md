---
doc: rag-pipeline/hybrid-search
description: pgvector cosine + Postgres FTS fused with RRF; carries the HNSW halfvec performance contract.
owns:
  - retrieval/hybrid_search.py
related:
  - rag-pipeline/document-visibility
  - rag-pipeline/store-bias
  - platform/config-weights
  - platform/http-server
  - indexing/chunking-embedding
  - database/models
  - database/supabase-migrations
last_verified: 2026-07-31
stub: false
---

# hybrid-search — RRF rank-fusion over pgvector + FTS

**RRF rank-fusion of a semantic arm and a keyword arm (k=60), no per-arm
weight.** (Contrast `store-bias`, the post-fusion per-store-kind bias, and
`platform/config-weights`, the bias-weights config — see the retrieval-weights
disambiguation in `_index`.) The densest, most load-bearing file in the domain.

## Interface

- `class AITAHybridSearchRetriever(db_session, search_space_id)`
- `async hybrid_search(query_text, top_k=60, material_kind=None) -> list[dict]`
  — keys: `chunk_id, content, score, page_number, section_path, chunk_type,
  figure_id, document_id, doc_title, material_kind, week, metadata` (+ OCR/asset
  fields).

## Data flow

1. `embed_text(query_text)` (`indexing/chunking-embedding`) runs through
   `asyncio.to_thread` so the synchronous OpenAI client cannot block the event
   loop — same model/dims as indexing; must not drift.
2. Resolve visible `document_id`s ONCE via `active_document_conditions`
   (`document-visibility`); return `[]` early if none visible.
3. Two module CTEs `_build_semantic_cte` / `_build_keyword_cte` — each wraps
   `ORDER BY distance` / `ts_rank` + `LIMIT n_results (=top_k*5)` in an inner
   subquery so `rank() OVER` computes over ≤n rows.
4. `SET LOCAL hnsw.*` from `_iterative_scan_statements()` before the fused query.
5. Outer query `FULL OUTER JOIN`s the CTEs scoring `1/(60+sem_rank)+1/(60+kw_rank)`.

## Invariants & gotchas

- **The semantic arm filters chunks by a MATERIALIZED integer array
  `document_id = ANY(:visible_ids)` — NOT a join, NOT `IN (subquery)`.** Only
  this form lets the HNSW index engage under `hnsw.iterative_scan` (pgvector ≥
  0.8). A join/subquery forces a brute-force `Sort`.
- **The semantic distance reads `document_chunks.embedding_halfvec` directly —
  no per-row cast (PR-4).** That column is a STORED generated column
  (`(embedding)::halfvec(EMBEDDING_DIM)`, `database/models.py`'s
  `DocumentChunk.embedding_halfvec`, a `Computed()` column the ORM never
  writes), computed once at write time and indexed by
  `document_chunks__embedding_halfvec_stored_hnsw__idx`
  (`supabase/migrations/20260731130000_pr4_hybrid_search_stored_halfvec.sql`).
  Only the query vector (a Python list, not a stored row) is cast, via
  `ExtensionsHalfVector`/`_compile_extensions_halfvec` in `database/models.py`
  (shared with the generated column's declared type), so halfvec resolves in
  the `extensions` schema. Prior form cast `embedding::halfvec(EMBEDDING_DIM)`
  per candidate row against an EXPRESSION index of the same shape; despite
  matching the expression, the planner chose a per-row-cast Seq/Bitmap Scan
  under the visibility filter anyway (0 scans of that index in staging
  pg_stat_user_indexes). Measured on a real-Postgres ~7.5k-row/~1.5k-selected
  fixture (`tests/database/test_pr4_hybrid_search_stored_halfvec.py`): even
  under an IDENTICAL scan-plan shape, reading the pre-computed column beats
  casting `embedding` per row by >10x (cast, not index absence, was the
  dominant cost) — see that test's `test_explain_analyze_plan_shapes_and_timing`.
  RRF fuses on rank, not raw distance, so the rewrite is proven
  rank-order-preserving, not just faster (same test module,
  `test_semantic_ranking_parity_*`).
- **The final fused SELECT never returns `document_chunks.embedding`,
  `document_chunks.embedding_halfvec`, or `documents.embedding` (PR-4).**
  Deferred via `sqlalchemy.orm.defer()`/`joinedload(...).defer()` at the query
  site — chunks_out never reads them, so returning them was pure per-row
  detoast waste (~12KB `vector`/~6KB `halfvec` per row).
- `SET LOCAL` is transaction-scoped (autobegun session) — resets at
  commit/rollback, nothing leaks through the asyncpg pool.
- The keyword arm still joins `app.documents` (GIN FTS, not the cold bottleneck).
- Only the async retrieval call site offloads `embed_text`; the synchronous
  embedder itself remains unchanged for synchronous callers.
- Recall bounded by `scripts/eval_iterative_scan_recall.py` (top-20 overlap 1.000
  vs brute-force baseline) — unaffected by PR-4, which only changes where the
  halfvec value comes from, not the distance/ranking math.
- The old expression index (`document_chunks__embedding_halfvec_hnsw__idx`) is
  DROPPED as of the PR-4 migration. The two legacy SQL RPCs that still
  reference it (`internal.hybrid_search()` / `internal.fetch_items()`,
  `20260717050000_retrieval_functions_v1.sql`) are dead code — no
  `.rpc(`/PostgREST call site anywhere in this repo — and were deliberately
  NOT repointed at the new column; see the PR-4 migration header before
  reviving either.

## Env flags

`HNSW_ITERATIVE_SCAN` (relaxed_order | strict_order | off), `HNSW_EF_SEARCH`,
`HNSW_MAX_SCAN_TUPLES`, `EMBEDDING_DIM`.

## Related

`document-visibility`, `store-bias`, `platform/config-weights`,
`platform/http-server` (teacher weight-override seam); ORM `Document`/
`DocumentChunk`/`ExtensionsHalfVector` in `database/models`;
`database/supabase-migrations` (PR-4 migration, DDL authority for
`embedding_halfvec` + its index).
