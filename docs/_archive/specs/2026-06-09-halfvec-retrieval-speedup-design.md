# P0: `halfvec` Cast in the Semantic Search Arm — Design

**Date:** 2026-06-09
**Branch:** RetrievalV2
**Status:** Approved — ready for implementation plan
**Scope:** One-file change to `retrieval/hybrid_search.py`. No schema change, no migration, no new dependency.

---

## Problem (measured, not inferred)

The QA pipeline's retrieval stage was suspected to be slow. We instrumented the **live** database (read-only `EXPLAIN ANALYZE`) instead of guessing, and found the real bottleneck:

| Stage | Measured (space 1, 5,844 chunks) | Notes |
|---|---|---|
| **Semantic vector search** | **3,529 ms** | Brute-force scan + top-N heapsort on `embedding <=> query` |
| Same query, operands cast to `halfvec` | **107 ms** | **33× faster, identical ranking order** |
| FTS (lexical) arm | 6.7 ms | GIN index already used — fine, untouched |

### Root cause

`retrieval/hybrid_search.py` builds the semantic CTE ordering by the raw `vector(3072)` distance:

```python
AITAChunk.embedding.op("<=>")(query_embedding)
```

But the only vector index in production is built on the **`halfvec` cast** of the column:

```
idx_aita_chunks_embedding_hnsw
  ON public.aita_chunks USING hnsw (((embedding)::halfvec(3072)) halfvec_cosine_ops)
```

The query expression (`vector <=>`) and the index expression (`halfvec <=>`) don't match, so PostgreSQL cannot use the index and brute-forces cosine distance across every chunk in the class.

Two contributing facts uncovered during investigation:
- The `ENABLE_HNSW = EMBEDDING_DIM <= 2000` dead-code path in `database/migrations/001_create_schema.py` means the migration never creates a chunk vector index at 3072 dims — the working `halfvec` index was added out-of-band (mirroring the pattern in `database/migrations/019_apollo_misconceptions.sql`).
- The 3,529 ms → 107 ms win comes **purely from computing distance in `halfvec` (16-bit, 6 KB/vector) instead of `vector` (32-bit, 12 KB/vector)**. In the EXPLAIN, the corrected query is *still* an exact brute-force scan — the HNSW index is *still* not engaged (the `document_id` pre-filter blocks it without `hnsw.iterative_scan`). Engaging the index is explicitly deferred (see Out of Scope). P0 needs no index engagement to deliver 33×.

---

## The Change

Single file: `retrieval/hybrid_search.py`, inside the `semantic_cte` only.

Replace the distance expression in **both** places it appears — the ranking window and the ordering — so they compute distance in `halfvec`, matching the column's stored embedding cast:

Conceptually:
```
(AITAChunk.embedding :: halfvec(DIM))  <=>  (query_embedding :: halfvec(DIM))
```

Implementation notes:
- `HALFVEC` is available: `from pgvector.sqlalchemy import HALFVEC`.
- Left operand: `AITAChunk.embedding.cast(HALFVEC(DIM))`.
- Right operand: `cast(query_embedding, HALFVEC(DIM))` (SQLAlchemy `cast`).
- `DIM` is sourced from the same `EMBEDDING_DIM` config used by the models (`database/models.py` → `Vector(EMBEDDING_DIM)`), so the cast stays matched to the index width (3072). Do not hardcode 3072 in two places — reuse the existing config constant.
- Both the `func.rank().over(order_by=...)` expression and the `.order_by(...)` must use the **identical** halfvec expression so rank and ordering stay consistent.

**Untouched:** the keyword CTE, RRF fusion (`_RRF_K`, the FULL OUTER JOIN scoring), the reranker, store-bias, and context packing. This change is confined to how the semantic arm computes distance.

---

## Why It's Safe

- **RRF fuses on rank, not raw distance.** The final score is `1/(k + sem_rank) + 1/(k + kw_rank)`. Absolute halfvec distance values never enter fusion — only the *ordering* of the semantic arm does, and ordering is preserved (verified: the 107 ms query returns the same top results in the same order).
- **It matches the index's intended design.** A `halfvec(3072)` HNSW index already exists; the query simply never addressed it. We are aligning the query to the schema, not reshaping retrieval.
- **The only behavioral delta** is 16-bit vs 32-bit float precision in the semantic arm's tie-breaking — negligible, and exactly the precision the existing index was built to use.

---

## Validation Plan

Per CLAUDE.md ("Do not change the hybrid search fusion logic without running the full retrieval test suite first"):

1. **Retrieval test suite stays green:**
   `pytest tests/ -k "retriev or hybrid or search" -v` plus `tests/integration/test_pgvector_harness.py`.
2. **Regression guard:** add a unit assertion that the compiled semantic CTE SQL contains the `halfvec` cast (so a future edit can't silently revert to the slow `vector` path).
3. **Re-measure:** re-run the read-only `EXPLAIN ANALYZE` (the diagnostic used during design) and confirm the semantic arm stays in the ~100 ms range, not multi-second.

---

## Out of Scope (Deferred)

- **P1 — Token streaming** of the answer over `/ask/stream` (perceived latency). Groundwork exists in the IndexerV2 worktree (`ai/streaming.py` `JsonStringFieldStreamer` + a full plan). Not touched here.
- **P2 — The pre-search `extract_and_filter_keywords` LLM call** (query expansion, ~1–3 s serial). Requires a retrieval-quality A/B before any change. Not touched here.
- **P3 — Actually engaging the HNSW index** via `hnsw.iterative_scan` (`strict_order`/`relaxed_order`) for when a class grows past tens of thousands of chunks. Brute-force `halfvec` at 107 ms is sufficient at current data size (largest class: 5,844 chunks). Introduces approximation (recall < 100%), so it is a separate, measured change.
- The solver (`solve_with_bundle`, `MAIN_MODEL=gpt-5`, `reasoning_effort=high`) is **sacred** — answer quality is not to be touched.

---

## Risk

Lowest tier. Single-file, single-arm change; exact ranking preserved; RRF inputs unchanged; gated behind the full retrieval test suite before merge. Rollback is reverting one commit.
