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
last_verified: 2026-07-25
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

1. `embed_text(query_text)` (`indexing/chunking-embedding`) — same model/dims as
   indexing; must not drift.
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
  this form lets the halfvec HNSW expression index engage under
  `hnsw.iterative_scan` (pgvector ≥ 0.8). A join/subquery forces a brute-force
  `Sort` that detoasts every embedding (measured cold ~3.4 s / ~27.6k pages vs
  ~0.75 s / ~5.8k pages with the index).
- Both operands cast to `halfvec(EMBEDDING_DIM)` via
  `_ExtensionsHalfVector`/`_compile_extensions_halfvec` so halfvec resolves in
  the `extensions` schema and distance runs in 16-bit. RRF fuses on rank, not
  raw distance, so fusion is unaffected.
- `SET LOCAL` is transaction-scoped (autobegun session) — resets at
  commit/rollback, nothing leaks through the asyncpg pool.
- The keyword arm still joins `app.documents` (GIN FTS, not the cold bottleneck).
- Recall bounded by `scripts/eval_iterative_scan_recall.py` (top-20 overlap 1.000
  vs brute-force baseline).

## Env flags

`HNSW_ITERATIVE_SCAN` (relaxed_order | strict_order | off), `HNSW_EF_SEARCH`,
`HNSW_MAX_SCAN_TUPLES`, `EMBEDDING_DIM`.

## Related

`document-visibility`, `store-bias`, `platform/config-weights`,
`platform/http-server` (teacher weight-override seam); ORM `Document`/
`DocumentChunk` in `database/models`.
