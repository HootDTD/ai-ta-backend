"""Regression guard: the semantic distance expression must compute in halfvec.

If this ever reverts to raw `vector` distance, the query stops matching
idx_aita_chunks_embedding_hnsw and the semantic scan goes from ~0.1s back to ~3.5s.
Compile-only — no database required.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from database.models import AITAChunk, EMBEDDING_DIM
from retrieval.hybrid_search import (
    _halfvec_cosine_distance,
    _build_semantic_cte,
    _build_keyword_cte,
)

pytestmark = pytest.mark.unit


def _sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect())).lower()


def _compile_full(selectable) -> str:
    return str(
        select(selectable).compile(dialect=postgresql.dialect())
    ).lower()


def test_both_operands_cast_to_halfvec():
    expr = _halfvec_cosine_distance([0.1] * EMBEDDING_DIM)
    sql = _sql(expr)
    assert "halfvec" in sql
    # Both the column and the query vector must be cast (matches the index expression).
    assert sql.count(f"halfvec({EMBEDDING_DIM})") >= 2
    assert f"halfvec({EMBEDDING_DIM})" in sql


def test_uses_cosine_distance_operator():
    expr = _halfvec_cosine_distance([0.1] * EMBEDDING_DIM)
    assert "<=>" in _sql(expr)


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
