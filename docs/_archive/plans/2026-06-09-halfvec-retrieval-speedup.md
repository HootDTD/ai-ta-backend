# P0: halfvec Cast in Semantic Search — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the semantic retrieval arm from ~3,529 ms to ~107 ms by computing cosine distance in `halfvec(3072)` so the query matches the existing `idx_aita_chunks_embedding_hnsw` index, with zero change to ranking order.

**Architecture:** One production file changes (`retrieval/hybrid_search.py`). A small DRY helper builds the `halfvec`-cast distance expression; the semantic CTE uses it in both the ranking window and the `ORDER BY`. The keyword CTE, RRF fusion, reranker, and context packing are untouched. A fast unit test compiles the helper's SQL and asserts the `halfvec` cast is present (regression guard); the existing retrieval suite proves behavior is preserved.

**Tech Stack:** Python, SQLAlchemy (PostgreSQL dialect), pgvector (`pgvector.sqlalchemy.HALFVEC`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-09-halfvec-retrieval-speedup-design.md`

---

## File Structure

- `retrieval/hybrid_search.py` — **modify.** Add imports (`cast`, `HALFVEC`, `EMBEDDING_DIM`), add module-level helper `_halfvec_cosine_distance()`, use it in the `semantic_cte` (lines 66 and 71 today).
- `tests/unit/test_hybrid_search_halfvec.py` — **create.** Compile-only unit test asserting both operands are cast to `halfvec` and the `<=>` operator is used. No DB needed.

Reference (current `retrieval/hybrid_search.py`):
- Line 15: `from sqlalchemy import func, select, text`
- Line 19: `from database.models import AITAChunk, AITADocument`
- Line 48: `query_embedding = embed_text(query_text)`
- Lines 62–73: the `semantic_cte` block, using `AITAChunk.embedding.op("<=>")(query_embedding)` at line 66 (`.over(order_by=...)`) and line 71 (`.order_by(...)`).

---

### Task 1: Add the `halfvec` distance helper (TDD)

**Files:**
- Create: `tests/unit/test_hybrid_search_halfvec.py`
- Modify: `retrieval/hybrid_search.py:15` (imports), `retrieval/hybrid_search.py:19` (imports), and add a module-level helper after the imports.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_hybrid_search_halfvec.py`:

```python
"""Regression guard: the semantic distance expression must compute in halfvec.

If this ever reverts to raw `vector` distance, the query stops matching
idx_aita_chunks_embedding_hnsw and the semantic scan goes from ~0.1s back to ~3.5s.
Compile-only — no database required.
"""
from __future__ import annotations

from sqlalchemy.dialects import postgresql

from database.models import EMBEDDING_DIM
from retrieval.hybrid_search import _halfvec_cosine_distance


def _sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect())).lower()


def test_both_operands_cast_to_halfvec():
    expr = _halfvec_cosine_distance([0.1] * EMBEDDING_DIM)
    sql = _sql(expr)
    assert "halfvec" in sql
    # Both the column and the query vector must be cast (matches the index expression).
    assert sql.count("cast(") >= 2
    assert f"halfvec({EMBEDDING_DIM})" in sql


def test_uses_cosine_distance_operator():
    expr = _halfvec_cosine_distance([0.1] * EMBEDDING_DIM)
    assert "<=>" in _sql(expr)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_hybrid_search_halfvec.py -v`
Expected: FAIL with `ImportError: cannot import name '_halfvec_cosine_distance' from 'retrieval.hybrid_search'`.

- [ ] **Step 3: Add imports**

In `retrieval/hybrid_search.py`, change line 15 from:

```python
from sqlalchemy import func, select, text
```

to:

```python
from sqlalchemy import cast, func, select, text
```

And change line 19 from:

```python
from database.models import AITAChunk, AITADocument
```

to:

```python
from database.models import AITAChunk, AITADocument, EMBEDDING_DIM
```

Then add this import alongside the other `from .` / pgvector imports near the top (after line 19):

```python
from pgvector.sqlalchemy import HALFVEC
```

- [ ] **Step 4: Add the helper**

In `retrieval/hybrid_search.py`, immediately after the `_RRF_K = 60` constant (around line 26), add:

```python


def _halfvec_cosine_distance(query_embedding):
    """Cosine distance computed in ``halfvec(EMBEDDING_DIM)``.

    The production vector index is ``idx_aita_chunks_embedding_hnsw`` on
    ``(embedding::halfvec(3072)) halfvec_cosine_ops``. Casting BOTH operands to
    halfvec makes the query expression match that index and, more importantly,
    runs the distance math in 16-bit (6 KB/vector) instead of 32-bit
    (12 KB/vector) — measured 3,529 ms -> 107 ms on the largest class, with
    identical ranking order. RRF fuses on rank, not raw distance, so fusion is
    unaffected.
    """
    return AITAChunk.embedding.cast(HALFVEC(EMBEDDING_DIM)).op("<=>")(
        cast(query_embedding, HALFVEC(EMBEDDING_DIM))
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_hybrid_search_halfvec.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add retrieval/hybrid_search.py tests/unit/test_hybrid_search_halfvec.py
git commit -m "feat(retrieval): add halfvec cosine-distance helper for semantic CTE"
```

---

### Task 2: Use the helper in the semantic CTE

**Files:**
- Modify: `retrieval/hybrid_search.py:66` and `retrieval/hybrid_search.py:71` (inside the `semantic_cte`).

- [ ] **Step 1: Replace the ranking-window distance expression**

In `retrieval/hybrid_search.py`, find (line 66, inside `func.rank().over(...)`):

```python
                .over(order_by=AITAChunk.embedding.op("<=>")(query_embedding))
```

Replace with:

```python
                .over(order_by=_halfvec_cosine_distance(query_embedding))
```

- [ ] **Step 2: Replace the ORDER BY distance expression**

In the same `semantic_cte` block, find (line 71):

```python
            .order_by(AITAChunk.embedding.op("<=>")(query_embedding))
```

Replace with:

```python
            .order_by(_halfvec_cosine_distance(query_embedding))
```

- [ ] **Step 3: Confirm no other `op("<=>")` remains in the semantic path**

Run: `grep -n 'embedding.op("<=>")' retrieval/hybrid_search.py`
Expected: no output (both call sites now use the helper). The keyword CTE uses `ts_rank_cd`, not `<=>`, so it is unaffected.

- [ ] **Step 4: Run the retrieval test suite (CLAUDE.md mandate)**

Run: `pytest tests/test_retrieval.py tests/functions-tests/test_retrieval_metadata.py tests/unit/test_hybrid_search_halfvec.py -v --tb=short`
Expected: PASS / same skip pattern as before this change (no new failures).

Then run the integration harness (skips cleanly if Docker/Postgres is unavailable):

Run: `pytest tests/integration/test_pgvector_harness.py -v --tb=short`
Expected: PASS, or SKIPPED if no Docker — either is acceptable; it must not FAIL.

- [ ] **Step 5: Commit**

```bash
git add retrieval/hybrid_search.py
git commit -m "perf(retrieval): compute semantic distance in halfvec to match HNSW index

Semantic CTE now orders by (embedding::halfvec(3072)) <=> query::halfvec(3072),
matching idx_aita_chunks_embedding_hnsw. Measured 3529ms -> 107ms; ranking order
preserved (RRF fuses on rank). Refs spec 2026-06-09-halfvec-retrieval-speedup."
```

---

### Task 3: Re-measure on the live DB (read-only verification)

This confirms the production win holds. It is read-only (a `SELECT`/`EXPLAIN`); it makes no writes and is not committed.

**Files:** none (ad-hoc script run from `/tmp`).

- [ ] **Step 1: Write the read-only EXPLAIN script**

Create `/tmp/verify_halfvec.py`:

```python
"""Read-only: confirm the semantic search now runs in ~100ms. No writes."""
import asyncio, re, ssl, random

ENV = "/Users/ishaanbatra/Documents/GitHub/ai-ta-backend/.env"


def load_dsn():
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if line.startswith("SUPABASE_DB_URL="):
                dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    dsn = re.sub(r"^(postgres(ql)?)\+\w+://", r"postgresql://", dsn)
    return dsn.split("?", 1)[0]


async def main():
    import asyncpg
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    conn = await asyncpg.connect(load_dsn(), ssl=ctx, statement_cache_size=0, timeout=30)
    try:
        space = await conn.fetchval(
            """select d.search_space_id
               from aita_chunks c join aita_documents d on c.document_id=d.id
               group by d.search_space_id order by count(*) desc limit 1"""
        )
        rnd = random.Random(7)
        vlit = "[" + ",".join(f"{rnd.uniform(-0.05,0.05):.5f}" for _ in range(3072)) + "]"
        q = f"""
        EXPLAIN (ANALYZE, BUFFERS, TIMING)
        SELECT c.id
        FROM aita_chunks c
        JOIN aita_documents d ON c.document_id = d.id
        WHERE d.search_space_id = {int(space)}
          AND d.status->>'state' = 'ready'
        ORDER BY (c.embedding::halfvec(3072)) <=> '{vlit}'::halfvec(3072)
        LIMIT 100
        """
        for r in await conn.fetch(q):
            line = re.sub(r"'\[[-0-9.,e ]+\]'", "'<vec>'", r["QUERY PLAN"])
            print(line)
    finally:
        await conn.close()


asyncio.run(main())
```

- [ ] **Step 2: Run it and confirm the execution time**

Run: `python /tmp/verify_halfvec.py`
Expected: the final `Execution Time:` line reads roughly `100–150 ms` (not multi-second). Baseline before the fix was 3,529 ms.

- [ ] **Step 3: Clean up**

Run: `rm /tmp/verify_halfvec.py`

---

## Self-Review

**Spec coverage:**
- "Cast both operands to halfvec in the semantic CTE (rank + order_by)" → Task 1 (helper) + Task 2 (both call sites). ✓
- "Source DIM from EMBEDDING_DIM config, no double hardcode" → Task 1 Step 3/4 imports and uses `EMBEDDING_DIM`. ✓
- "Keyword CTE / RRF / reranker / packing untouched" → Task 2 Step 3 grep confirms only semantic `<=>` changed. ✓
- "Regression guard: compiled SQL contains the halfvec cast" → Task 1 unit test. ✓
- "Run full retrieval test suite" → Task 2 Step 4. ✓
- "Re-run EXPLAIN ANALYZE to confirm the drop" → Task 3. ✓
- Out-of-scope items (streaming, keyword call, HNSW iterative_scan) → not present in any task. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full content; exact line numbers and commands given. ✓

**Type/name consistency:** Helper named `_halfvec_cosine_distance` consistently in the implementation (Task 1 Step 4), the unit test import (Task 1 Step 1), and both call sites (Task 2 Steps 1–2). `HALFVEC` and `EMBEDDING_DIM` imports match their usage. Compiled-SQL assertions (`halfvec`, `cast(`, `<=>`, `halfvec(3072)`) match the verified compile output `CAST(aita_chunks.embedding AS HALFVEC(3072)) <=> CAST(:param AS HALFVEC(3072))`. ✓
