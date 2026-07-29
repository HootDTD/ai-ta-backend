# QA Pipeline Latency Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut end-to-end /ask/stream latency from ~60s to ~30s by fixing the HNSW-defeating hybrid search SQL, parallelizing snippet scoring fully, moving auxiliary LLM calls to faster models, and enabling prompt caching on the solver — all without changing answer quality or citation semantics.

**Architecture:** Four independent fixes, each separately testable and revertible. (1) Restructure the hybrid-search CTEs so the pgvector HNSW index can be used (`ORDER BY <distance> LIMIT n` inner subquery, rank window computed over only the limited candidates). (2) Add the missing halfvec HNSW index migration so the DB schema is reproducible. (3) Config-level speedups: full-width citation scoring pool, mini models for keyword extraction and snippet scoring. (4) OpenAI request-level latency options on the solver: `prompt_cache_key`, optional `service_tier`, optional `verbosity`.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async + asyncpg, pgvector (halfvec/HNSW) on Supabase PostgreSQL, OpenAI Responses + Chat Completions APIs, pytest.

**Branch:** Work happens on the current `RetrievalV2` branch. NEVER push to `main`.

**Guardrails (from CLAUDE.md + retrieval-tuning skill):**
- Tasks 1–2 touch the hybrid search fusion path → the full retrieval test suite (`pytest tests/test_retrieval.py tests/test_full_pipeline.py tests/unit -v`) MUST pass before each commit in those tasks.
- After Tasks 1, 4, and 5, manually test with 3 query types (factual, conceptual, equation-based) and compare retrieval/citation scores before vs. after.
- Citation marker generation must not change — none of these tasks touch `context_packer.py`; if you find yourself editing it, stop.
- Do NOT modify `.env` (only `.env.example` documentation). Do NOT install new packages.

**Background (why each fix):**
- `hybrid_search_sql=6.6s` measured live, but the halfvec expression index gives ~107ms when hit (see docstring at `retrieval/hybrid_search.py:31`). The current CTEs compute `rank() OVER (ORDER BY distance)` — a window function evaluates over **all** filtered rows before `LIMIT`, so Postgres computes the 3072-dim distance for every chunk in the search space and cannot use the HNSW index (known pgvector limitation: pgvector issues #702/#703).
- `idx_aita_chunks_embedding_hnsw` exists only on the live DB (created by hand); `database/migrations/001_create_schema.py:82` skips HNSW for 3072 dims (`ENABLE_HNSW = EMBEDDING_DIM <= 2000`, predates halfvec). Schema drift risk.
- `snippet_scoring=8.5s n=20` with `CITATION_WORKERS` default 12 → 20 calls run in two waves. One wave ≈ slowest single call.
- `keyword_extraction=3.8s` is one gpt-4o call whose output only *hints* the combined query ("bad keyword extraction doesn't kill retrieval" — `retrieval/pipeline.py:58`). A mini model is sufficient.
- `solve_stream=40s`: static system prompt (`tutor_prompt()`) is already first via `instructions`, so prompt caching works once requests route to the same cache (`prompt_cache_key`). `service_tier`/`verbosity` are optional env-gated knobs.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `retrieval/hybrid_search.py` | Modify | Restructure semantic + keyword CTEs into index-friendly helpers |
| `tests/unit/test_hybrid_search_halfvec.py` | Modify | Extend compile-only regression guards for index-friendly SQL shape |
| `database/migrations/022_chunks_halfvec_hnsw.sql` | Create | Halfvec HNSW expression index on `aita_chunks.embedding` |
| `ai/main_ai.py` | Modify | `_citation_pool_size` helper; model env fallbacks; solver request options |
| `tests/unit/test_citation_pool_size.py` | Create | Unit tests for pool sizing |
| `tests/functions-tests/test_model_selection.py` | Create | Fake-client tests for keyword/scorer model env vars |
| `tests/functions-tests/test_solve_stream.py` | Modify | Tests for `prompt_cache_key` / `service_tier` / `verbosity` kwargs |
| `.env.example` | Modify | Document new env vars |

---

### Task 1: Index-friendly hybrid search CTEs

The fix: compute the vector distance / FTS rank inside an inner subquery that is exactly `SELECT … ORDER BY <expr> LIMIT n` (the only shape pgvector's HNSW index scan matches), then compute the RRF rank with a window function **over the ≤300 candidates**, not the whole table. Output columns of both CTEs (`id`, `rank`) are unchanged, so the RRF fusion query is untouched.

**Files:**
- Modify: `retrieval/hybrid_search.py:81-110`
- Test: `tests/unit/test_hybrid_search_halfvec.py`

- [ ] **Step 1: Write the failing compile-only tests**

Append to `tests/unit/test_hybrid_search_halfvec.py`:

```python
from retrieval.hybrid_search import (
    _build_semantic_cte,
    _build_keyword_cte,
)
from sqlalchemy import func, select
from database.models import AITAChunk


def _compile_full(selectable) -> str:
    return str(
        select(selectable).compile(dialect=postgresql.dialect())
    ).lower()


def test_semantic_cte_limits_before_window():
    """The distance ORDER BY + LIMIT must live in an inner subquery so the
    HNSW index scan applies; rank() runs over only the limited candidates.

    Distinguishing shape (NOT textual position — OVER precedes LIMIT in both
    the broken and fixed SQL): the rank window must order by the materialized
    `distance` column, never by the raw `<=>` expression, and the candidates
    must come from an inner `FROM (SELECT ... LIMIT n)` subquery.
    """
    cte = _build_semantic_cte([0.1] * EMBEDDING_DIM, [], n_results=300)
    sql = _compile_full(cte)
    # The window orders by the subquery's distance column, not the <=> expr.
    over_clause = sql.split("over (", 1)[1].split(")", 1)[0]
    assert "<=>" not in over_clause, (
        "rank() must window over the materialized distance column; windowing "
        "over the <=> expression forces a full-table distance scan (no HNSW)"
    )
    assert "distance" in over_clause
    # Candidates come from an inner LIMITed subquery (the HNSW-matchable shape).
    assert "from (select" in sql, "expected inner candidates subquery"
    inner_sql = sql.split("from (select", 1)[1].split("semantic_candidates", 1)[0]
    assert "<=>" in inner_sql
    assert "limit" in inner_sql, "inner candidates subquery must LIMIT"


def test_keyword_cte_limits_before_window():
    tsvector = func.to_tsvector("english", AITAChunk.content)
    tsquery = func.plainto_tsquery("english", "bernoulli equation")
    cte = _build_keyword_cte(tsvector, tsquery, [], n_results=300)
    sql = _compile_full(cte)
    over_clause = sql.split("over (", 1)[1].split(")", 1)[0]
    assert "ts_rank_cd" not in over_clause
    assert "text_rank" in over_clause
    assert "from (select" in sql
    inner_sql = sql.split("from (select", 1)[1].split("keyword_candidates", 1)[0]
    assert "@@" in inner_sql  # GIN-indexable FTS match filter preserved
    assert "limit" in inner_sql


def test_semantic_cte_still_uses_halfvec_both_operands():
    cte = _build_semantic_cte([0.1] * EMBEDDING_DIM, [], n_results=300)
    sql = _compile_full(cte)
    assert sql.count(f"halfvec({EMBEDDING_DIM})") >= 2
    assert "<=>" in sql
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_hybrid_search_halfvec.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_semantic_cte'`

- [ ] **Step 3: Implement the CTE builders**

In `retrieval/hybrid_search.py`, add module-level helpers after `_halfvec_cosine_distance` (before the class):

```python
def _build_semantic_cte(query_embedding, base_conditions, n_results: int):
    """Index-friendly semantic candidates CTE.

    The inner subquery is exactly ``SELECT … ORDER BY <distance> LIMIT n`` —
    the only query shape pgvector's HNSW scan matches. The rank() window runs
    over the ≤n_results candidates only. Putting the window in the same scope
    as the distance ORDER BY forces a full-table distance scan (measured
    6.6s vs ~0.1s on the largest class) because window functions evaluate
    before LIMIT.
    """
    distance = _halfvec_cosine_distance(query_embedding)
    inner = (
        select(AITAChunk.id.label("id"), distance.label("distance"))
        .join(AITADocument, AITAChunk.document_id == AITADocument.id)
        .where(*base_conditions)
        .order_by(distance)
        .limit(n_results)
        .subquery("semantic_candidates")
    )
    return (
        select(
            inner.c.id,
            func.rank().over(order_by=inner.c.distance).label("rank"),
        )
        .cte("semantic_search")
    )


def _build_keyword_cte(tsvector, tsquery, base_conditions, n_results: int):
    """Index-friendly FTS candidates CTE (same inner-LIMIT-then-rank shape)."""
    text_rank = func.ts_rank_cd(tsvector, tsquery)
    inner = (
        select(AITAChunk.id.label("id"), text_rank.label("text_rank"))
        .join(AITADocument, AITAChunk.document_id == AITADocument.id)
        .where(*base_conditions)
        .where(tsvector.op("@@")(tsquery))
        .order_by(text_rank.desc())
        .limit(n_results)
        .subquery("keyword_candidates")
    )
    return (
        select(
            inner.c.id,
            func.rank().over(order_by=inner.c.text_rank.desc()).label("rank"),
        )
        .cte("keyword_search")
    )
```

Then in `hybrid_search()` replace the two CTE definitions (current lines 81-110) with:

```python
        # CTE 1: Semantic search (HNSW-index-friendly: LIMIT inside, rank outside)
        semantic_cte = _build_semantic_cte(query_embedding, base_conditions, n_results)

        # CTE 2: Keyword search (PostgreSQL FTS, same shape)
        keyword_cte = _build_keyword_cte(tsvector, tsquery, base_conditions, n_results)
```

The final RRF query (lines 114-136) is unchanged — it references `semantic_cte.c.id` / `semantic_cte.c.rank` / `keyword_cte.c.id` / `keyword_cte.c.rank`, which both builders still expose.

- [ ] **Step 4: Run the new unit tests**

Run: `pytest tests/unit/test_hybrid_search_halfvec.py -v`
Expected: PASS (all 5 tests, including the 2 pre-existing ones)

- [ ] **Step 5: Run the full retrieval test suite (required by retrieval-tuning skill)**

Run: `pytest tests/test_retrieval.py tests/test_full_pipeline.py tests/unit -v --tb=short`
Expected: PASS. If any fusion/ordering test fails, STOP — do not weaken assertions; the rank semantics must be identical (rank() over the top-300 candidates orders the same as rank() over all rows for those candidates, since ranks beyond 300 never contributed to RRF top_k=60 anyway).

- [ ] **Step 6: Live verification — timing + EXPLAIN**

Start the server (`python server.py` or the uvicorn command), ask one question in each of the 3 required query types (factual, conceptual, equation-based) against the largest class, and record the `[timing] hybrid_search_sql=` lines.

Expected: `hybrid_search_sql` drops from ~6.6s to **< 0.5s** on warm runs.

Optionally confirm the plan directly (replace `:emb` with a real query vector and `:sid` with the search space id):

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT c.id, c.embedding::halfvec(3072) <=> :emb::halfvec(3072) AS distance
FROM aita_chunks c JOIN aita_documents d ON c.document_id = d.id
WHERE d.search_space_id = :sid
ORDER BY distance LIMIT 300;
```

Expected: `Index Scan using idx_aita_chunks_embedding_hnsw`. If you see `Seq Scan`, check the index exists (Task 2) and that the cast expression matches the index expression exactly.

Log the before/after retrieval scores for the 3 test queries (skill requirement) — the top-20 chunk ids should be identical or near-identical to the pre-change ordering.

- [ ] **Step 7: Commit and push**

```bash
git add retrieval/hybrid_search.py tests/unit/test_hybrid_search_halfvec.py
git commit -m "perf(retrieval): restructure hybrid CTEs so HNSW index scan applies

rank() window over the full filtered set forced a per-row 3072-dim
distance computation (6.6s). Inner ORDER BY distance LIMIT n subquery
matches the halfvec HNSW index; rank now windows over <=300 candidates.
RRF fusion query and output columns unchanged."
git push origin RetrievalV2
```

---

### Task 2: Migration for the halfvec HNSW index

The live DB has a hand-created `idx_aita_chunks_embedding_hnsw`; no migration creates it (001 skips HNSW for 3072 dims). Make the schema reproducible. `IF NOT EXISTS` makes this a no-op on the live DB.

**Files:**
- Create: `database/migrations/022_chunks_halfvec_hnsw.sql`

- [ ] **Step 1: Write the migration**

Create `database/migrations/022_chunks_halfvec_hnsw.sql`:

```sql
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
```

- [ ] **Step 2: Apply to the dev database and verify**

Apply the same way prior `.sql` migrations (004-021) were applied — psql against the Supabase connection string:

```bash
psql "$DATABASE_URL" -f database/migrations/022_chunks_halfvec_hnsw.sql
psql "$DATABASE_URL" -c "\d aita_chunks" | grep hnsw
```

Expected: the second command prints `idx_aita_chunks_embedding_hnsw` with `hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)`. (On the live DB the CREATE is a no-op because the index already exists — that's the point.)

- [ ] **Step 3: Commit and push**

```bash
git add database/migrations/022_chunks_halfvec_hnsw.sql
git commit -m "feat(db): migration 022 — codify halfvec HNSW index on aita_chunks

Index existed only on the live DB (created by hand); 001 skips HNSW for
3072 dims. IF NOT EXISTS makes it idempotent against prod."
git push origin RetrievalV2
```

---

### Task 3: Full-width citation scoring pool

`_prepare_solve_prompt` caps the scorer thread pool at `CITATION_WORKERS=12` while n=20 snippets → two waves (8.5s instead of ~4s). Extract a testable helper and raise the default cap to 24.

**Files:**
- Modify: `ai/main_ai.py:998-1002`
- Create: `tests/unit/test_citation_pool_size.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_citation_pool_size.py`:

```python
"""_citation_pool_size: one scoring wave by default, env-cappable."""
from __future__ import annotations

import pytest

from ai.main_ai import _citation_pool_size

pytestmark = pytest.mark.unit


def test_default_cap_covers_default_snippet_count(monkeypatch):
    monkeypatch.delenv("CITATION_WORKERS", raising=False)
    # K_SEM default is 20 snippets — all must score in a single wave.
    assert _citation_pool_size(20) == 20


def test_pool_never_exceeds_snippet_count(monkeypatch):
    monkeypatch.delenv("CITATION_WORKERS", raising=False)
    assert _citation_pool_size(5) == 5


def test_env_override_caps_pool(monkeypatch):
    monkeypatch.setenv("CITATION_WORKERS", "8")
    assert _citation_pool_size(20) == 8


def test_zero_snippets_still_returns_valid_pool_size(monkeypatch):
    monkeypatch.delenv("CITATION_WORKERS", raising=False)
    assert _citation_pool_size(0) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_citation_pool_size.py -v`
Expected: FAIL with `ImportError: cannot import name '_citation_pool_size'`

- [ ] **Step 3: Implement the helper and use it**

In `ai/main_ai.py`, add near `_score_and_answer_snippet` (module level):

```python
def _citation_pool_size(n_snippets: int) -> int:
    """Thread pool width for parallel snippet scoring.

    Default cap 24 > default snippet count (K_SEM=20) so all snippets score
    in a single wave — wall time ≈ slowest single call instead of two waves.
    """
    cap = int(os.getenv("CITATION_WORKERS", "24"))
    return max(1, min(cap, n_snippets))
```

Replace the inline computation at `ai/main_ai.py:999-1002`:

```python
    # OLD:
    # max_workers = min(
    #     int(os.getenv("CITATION_WORKERS", "12")),
    #     max(len(snippet_args), 1),
    # )
    max_workers = _citation_pool_size(len(snippet_args))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_citation_pool_size.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit and push**

```bash
git add ai/main_ai.py tests/unit/test_citation_pool_size.py
git commit -m "perf(main_ai): score all snippets in one wave (CITATION_WORKERS 12->24)

20 snippets / 12 workers ran in two waves (8.5s). Single wave ~= slowest
call. Extracted _citation_pool_size for testability."
git push origin RetrievalV2
```

---

### Task 4: Faster keyword extraction model

`extract_and_filter_keywords` (`ai/main_ai.py:788-789`) uses `PARSER_MODEL` (gpt-4o) and takes ~3.8s. Keywords are appended *hints* — the original question anchors retrieval (`retrieval/pipeline.py:58-59`) — so a mini model is sufficient. Introduce `KEYWORD_MODEL` defaulting to `gpt-4o-mini`, overridable back to gpt-4o if quality regresses.

**Files:**
- Modify: `ai/main_ai.py:788-789`
- Create: `tests/functions-tests/test_model_selection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/functions-tests/test_model_selection.py` (fake-client pattern copied from `tests/functions-tests/test_solve_stream.py`):

```python
"""Env-driven model selection for auxiliary LLM calls (keywords, snippet scoring)."""
from __future__ import annotations

import json
import types

import ai.main_ai as mai


def _fake_chat_client(captured: dict, content: str = "{}"):
    class _Completions:
        def create(self, *a, **k):
            captured.update(k)
            msg = types.SimpleNamespace(content=content)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice])
    chat = types.SimpleNamespace(completions=_Completions())
    return types.SimpleNamespace(chat=chat)


def test_keyword_extraction_defaults_to_mini_model(monkeypatch):
    monkeypatch.delenv("KEYWORD_MODEL", raising=False)
    monkeypatch.delenv("PARSER_MODEL", raising=False)
    captured: dict = {}
    payload = json.dumps({"context_summary": "x", "ranked_terms": []})
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    mai.extract_and_filter_keywords("What is Bernoulli's equation?", subject="Physics")
    assert captured["model"] == "gpt-4o-mini"


def test_keyword_extraction_env_override(monkeypatch):
    # Sentinel value, deliberately NOT the old default (gpt-4o) so this test
    # cannot pass coincidentally before KEYWORD_MODEL is wired up.
    monkeypatch.setenv("KEYWORD_MODEL", "sentinel-keyword-model")
    captured: dict = {}
    payload = json.dumps({"context_summary": "x", "ranked_terms": []})
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    mai.extract_and_filter_keywords("What is lift?", subject="Physics")
    assert captured["model"] == "sentinel-keyword-model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/functions-tests/test_model_selection.py -v`
Expected: BOTH tests FAIL — `test_keyword_extraction_defaults_to_mini_model` with `assert 'gpt-4o' == 'gpt-4o-mini'`, `test_keyword_extraction_env_override` with `assert 'gpt-4o' == 'sentinel-keyword-model'`.

- [ ] **Step 3: Implement**

In `ai/main_ai.py:789`, change the model lookup inside `extract_and_filter_keywords`:

```python
        resp = client.chat.completions.create(
            model=os.getenv("KEYWORD_MODEL", "gpt-4o-mini"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/functions-tests/test_model_selection.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Manual quality check (retrieval-tuning skill requirement)**

Start the server and run the 3 query types (factual, conceptual, equation-based). For each, compare the extracted `ranked_terms` and the `combined_query` diagnostic against a gpt-4o run (`KEYWORD_MODEL=gpt-4o`). Expected: substantially overlapping term lists and `[timing] keyword_extraction` dropping from ~3.8s to ~1-1.5s. If keyword quality visibly degrades (irrelevant terms dominating), keep `KEYWORD_MODEL=gpt-4o-mini` out of the default and set the default back to `gpt-4o` — then this task ships only the env hook.

- [ ] **Step 6: Commit and push**

```bash
git add ai/main_ai.py tests/functions-tests/test_model_selection.py
git commit -m "perf(main_ai): KEYWORD_MODEL env (default gpt-4o-mini) for keyword extraction

Keywords are appended hints only - the original question anchors
retrieval - so a mini model suffices. ~3.8s -> ~1.5s. Override with
KEYWORD_MODEL=gpt-4o to restore old behavior."
git push origin RetrievalV2
```

---

### Task 5: Faster snippet-scoring model

`_score_and_answer_snippet` defaults to gpt-4o per snippet (20 calls). Switch the default chain to `gpt-4o-mini`. This affects citation ordering and the `CITATION_SCORE_FLOOR` filter, so the manual before/after comparison is mandatory.

**Files:**
- Modify: `ai/main_ai.py:301-303` and `ai/main_ai.py:987-989`
- Test: `tests/functions-tests/test_model_selection.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/functions-tests/test_model_selection.py`:

```python
def test_snippet_scorer_defaults_to_mini_model(monkeypatch):
    monkeypatch.delenv("CITATION_SCORER_MODEL", raising=False)
    monkeypatch.delenv("PARSER_MODEL", raising=False)
    captured: dict = {}
    payload = json.dumps({
        "relevance": 0.9, "directness": 0.8, "score": 0.85,
        "context": "c", "answer": "a",
    })
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    snippet = types.SimpleNamespace(
        citation_marker="[S1]", page=3, why="", text="Bernoulli", section_path="", id="s1",
    )
    result = mai._score_and_answer_snippet("q?", snippet, 1.0, "bernoulli")
    assert captured["model"] == "gpt-4o-mini"
    assert result["marker"] == "[S1]"


def test_snippet_scorer_env_override(monkeypatch):
    # Sentinel value so this cannot pass coincidentally against the old default.
    monkeypatch.setenv("CITATION_SCORER_MODEL", "sentinel-scorer-model")
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, "{}"))
    snippet = types.SimpleNamespace(
        citation_marker="[S1]", page=None, why="", text="t", section_path="", id="s1",
    )
    mai._score_and_answer_snippet("q?", snippet, 1.0, "term")
    assert captured["model"] == "sentinel-scorer-model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/functions-tests/test_model_selection.py -v`
Expected: `test_snippet_scorer_defaults_to_mini_model` FAILS with `assert 'gpt-4o' == 'gpt-4o-mini'`

- [ ] **Step 3: Implement**

In `ai/main_ai.py:301-303` (`_score_and_answer_snippet`):

```python
    model = model or os.getenv("CITATION_SCORER_MODEL", "gpt-4o-mini")
```

In `ai/main_ai.py:987-989` (`_prepare_solve_prompt`):

```python
    scorer_model = os.getenv("CITATION_SCORER_MODEL", "gpt-4o-mini")
```

(Both drop the `PARSER_MODEL` fallback — `CITATION_SCORER_MODEL` is now the single override knob for scoring.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/functions-tests/test_model_selection.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Manual quality check — citation rankings before/after (mandatory)**

Run the 3 query types with `QA_DEBUG=1` twice: once with `CITATION_SCORER_MODEL=gpt-4o`, once with the new default. Compare `debug/main_ai_citation_rankings.json`:
- The top-5 markers should be substantially the same set.
- No relevant snippet should newly fall below `CITATION_SCORE_FLOOR=0.3`.
- `[timing] snippet_scoring` should drop to ~2-4s (combined with Task 3).

If the rankings degrade (relevant snippets dropped, ordering scrambled), revert the default to `gpt-4o` and ship only the env hook — Task 3's parallelism still nets most of the win.

- [ ] **Step 6: Commit and push**

```bash
git add ai/main_ai.py tests/functions-tests/test_model_selection.py
git commit -m "perf(main_ai): default snippet scorer to gpt-4o-mini

Per-snippet relevance/directness scoring is within mini-model capability;
verified citation_rankings parity on factual/conceptual/equation queries.
Override with CITATION_SCORER_MODEL=gpt-4o."
git push origin RetrievalV2
```

---

### Task 6: Solver request options — prompt cache key, service tier, verbosity

The static `tutor_prompt()` already leads the request as `instructions` (correct for prefix caching). Add `prompt_cache_key` so repeat requests route to the same cache machine, plus env-gated `service_tier` and `verbosity` knobs. Applied to both the streaming Responses path and the blocking Chat Completions path (verbosity is Responses-only here).

**Files:**
- Modify: `ai/main_ai.py:1331-1349` (`solve_with_bundle`) and `ai/main_ai.py:1371-1384` (`solve_with_bundle_stream`)
- Test: `tests/functions-tests/test_solve_stream.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/functions-tests/test_solve_stream.py`:

```python
def _minimal_events():
    return [
        _evt("response.output_text.delta", delta='{"steps": "x", "not_relevant": false}'),
        _evt("response.completed"),
    ]


def test_stream_sets_prompt_cache_key(monkeypatch):
    monkeypatch.delenv("PROMPT_CACHE_KEY", raising=False)
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(_minimal_events(), captured))
    monkeypatch.setattr(mai, "_prepare_solve_prompt", lambda *a, **k: ("SYS", "USER", "gpt-5"))
    list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    assert captured.get("prompt_cache_key") == "aita-solver:gpt-5"


def test_stream_service_tier_only_when_env_set(monkeypatch):
    captured: dict = {}
    monkeypatch.delenv("OPENAI_SERVICE_TIER", raising=False)
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(_minimal_events(), captured))
    monkeypatch.setattr(mai, "_prepare_solve_prompt", lambda *a, **k: ("SYS", "USER", "gpt-5"))
    list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    assert "service_tier" not in captured

    captured.clear()
    monkeypatch.setenv("OPENAI_SERVICE_TIER", "priority")
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(_minimal_events(), captured))
    list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    assert captured.get("service_tier") == "priority"


def test_stream_verbosity_only_when_env_set(monkeypatch):
    captured: dict = {}
    monkeypatch.delenv("MAIN_VERBOSITY", raising=False)
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(_minimal_events(), captured))
    monkeypatch.setattr(mai, "_prepare_solve_prompt", lambda *a, **k: ("SYS", "USER", "gpt-5"))
    list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    assert "verbosity" not in captured.get("text", {})

    captured.clear()
    monkeypatch.setenv("MAIN_VERBOSITY", "low")
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(_minimal_events(), captured))
    list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    assert captured["text"]["verbosity"] == "low"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest "tests/functions-tests/test_solve_stream.py" -v`
Expected: the 3 new tests FAIL (`prompt_cache_key` missing, etc.); the 3 pre-existing tests still PASS.

- [ ] **Step 3: Implement**

In `solve_with_bundle_stream` (`ai/main_ai.py`), after the existing `kwargs` dict is built and the reasoning/temperature branch (lines 1371-1384), add:

```python
    # Prompt-cache routing: tutor_prompt() instructions are the static prefix;
    # a stable cache key routes repeat requests to the same cache (per-key
    # throughput limit ~15 RPM — fine at current traffic).
    kwargs["prompt_cache_key"] = os.getenv(
        "PROMPT_CACHE_KEY", f"aita-solver:{model}"
    )
    service_tier = (os.getenv("OPENAI_SERVICE_TIER") or "").strip()
    if service_tier:
        kwargs["service_tier"] = service_tier
    verbosity = (os.getenv("MAIN_VERBOSITY") or "").strip()
    if verbosity and _is_reasoning_model(model):
        kwargs["text"]["verbosity"] = verbosity
```

In `solve_with_bundle` (`ai/main_ai.py:1331-1340`), inside `_chat` after the reasoning/temperature branch, add the same two cache/tier options (Chat Completions also supports them; verbosity stays Responses-only):

```python
        kwargs["prompt_cache_key"] = os.getenv(
            "PROMPT_CACHE_KEY", f"aita-solver:{model}"
        )
        service_tier = (os.getenv("OPENAI_SERVICE_TIER") or "").strip()
        if service_tier:
            kwargs["service_tier"] = service_tier
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest "tests/functions-tests/test_solve_stream.py" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Live verification**

Start the server, ask the same question twice in a row, and check the second run's `[timing] solve_stream`. Expected: cached-prefix runs show faster time-to-first-reasoning-delta (watch when the first SSE reasoning event arrives relative to the POST). Total solve time improvement varies with prefix length; do not block on a specific number — the assertion is "no errors, options accepted by the API."

- [ ] **Step 6: Commit and push**

```bash
git add ai/main_ai.py "tests/functions-tests/test_solve_stream.py"
git commit -m "perf(main_ai): prompt_cache_key + env-gated service_tier/verbosity on solver

Static tutor instructions already lead the prompt; a stable cache key
improves prefix-cache hit routing. OPENAI_SERVICE_TIER=priority and
MAIN_VERBOSITY=low are opt-in env knobs."
git push origin RetrievalV2
```

---

### Task 7: Document env vars + final verification

**Files:**
- Modify: `.env.example` (template only — never touch `.env`)

- [ ] **Step 1: Document the new/changed env vars**

Append to `.env.example` (adjust placement to match the file's existing grouping):

```bash
# --- Latency tuning (RetrievalV2) ---
# Parallel snippet-scoring pool width (default 24 = one wave for K_SEM=20)
#CITATION_WORKERS=24
# Model for keyword extraction (hints only; default gpt-4o-mini)
#KEYWORD_MODEL=gpt-4o-mini
# Model for per-snippet citation scoring (default gpt-4o-mini)
#CITATION_SCORER_MODEL=gpt-4o-mini
# Stable prompt-cache routing key for the solver (default aita-solver:<model>)
#PROMPT_CACHE_KEY=
# Opt-in: OpenAI priority processing for faster/more consistent solver tokens
#OPENAI_SERVICE_TIER=priority
# Opt-in: GPT-5 output verbosity (low|medium|high) on the streaming solver
#MAIN_VERBOSITY=low
```

- [ ] **Step 2: Run the entire test suite**

Run: `pytest tests/ -v --tb=short`
Expected: PASS (no regressions anywhere — chats, reports, router, integration).

- [ ] **Step 3: End-to-end timing comparison**

Start the server and ask the same question used in the original baseline. Record the full timing block and compare:

| Stage | Baseline | Target |
|---|---|---|
| keyword_extraction | 3.77s | ~1.5s |
| hybrid_search_sql | 6.61s | <0.5s |
| retrieval_total | 7.32s | <1.5s |
| snippet_scoring | 8.47s | ~2-4s |
| solve_stream | 40.2s | 25-35s (varies) |

If any stage misses badly, investigate before commit (systematic-debugging skill).

- [ ] **Step 4: Commit and push**

```bash
git add .env.example docs/superpowers/plans/2026-06-10-pipeline-latency-fixes.md
git commit -m "docs: document latency-tuning env vars + implementation plan"
git push origin RetrievalV2
```

---

## Explicitly Out of Scope (decided during analysis — do not add)

- **Dedicated reranker API (Cohere/Voyage/Jina)** to replace LLM snippet scoring: new package + provider — requires explicit user approval per CLAUDE.md, and changes citation-ranking semantics. Separate evaluated project.
- **Overlapping keyword extraction with embedding/search**: keywords feed the `combined_query` that is embedded, so overlap means embedding the raw question instead — a retrieval behavior change. Revisit only if keyword latency still matters after Task 4.
- **Splitting the solver into parallel sub-calls**: risks citation/coherence quality, which is the core product guarantee.
- **Difficulty-based model routing for the solver**: needs an eval harness first.

---

## Execution Amendments (2026-06-10)

Recorded during subagent-driven execution; the plan text above is preserved as written.

- **Task 1:** Live `EXPLAIN (ANALYZE, BUFFERS)` on the largest class (5,844 chunks): old CTE shape 3,938 ms → new shape **113 ms** via exact top-N heapsort. The HNSW index does NOT engage (visibility pre-filter blocks it without `hnsw.iterative_scan`) — recall unchanged; index engagement remains deferred (P3). Docstring updated to record the measured mechanism (commit ccdf3c2).
- **Task 4:** Default REVERTED to `gpt-4o` after live A/B (gpt-4o 1.33–2.14s vs gpt-4o-mini 2.97–4.20s — mini ~2x slower; more terms at lower tokens/s). `KEYWORD_MODEL` env hook shipped (commits 8494438, 05d6b96).
- **Task 5:** Mini switch DROPPED after live A/B (gpt-4o median 2.54s/call vs mini 3.30s, identical scores; parallel wall time = slowest call). Shipped: explicit `gpt-4o` default, PARSER_MODEL fallback removed from scorer resolution, regression tests (commit f03e516).
- **Task 7:** `.env.example` did not previously exist (CLAUDE.md referenced it); created scoped to verified variables rather than appended to.
- **Post-plan (staging merge):** `origin/staging` moved during execution (new CI workflow, ruff ratchet, test infra, architecture-doc tree with drift contract). Merged into RetrievalV2 (31c0697); our migration renumbered 022→023 (staging claimed 022 for the RLS stopgap); `_DummyOpenAI` gained a Responses-API stub for staging's e2e SSE test; branch-added files ruff-cleaned; `.gitignore` gained `!.env.example` (`.env.*` was silently ignoring the template — root cause of the file never existing); owner docs (rag-pipeline, domain-data, DATA-FLOW) amended per the drift contract (e25ffdd). CI gates verified locally: unit suite 148 passed, ruff added-files gate PASS, diff-cover patch coverage 83% (≥80%).
