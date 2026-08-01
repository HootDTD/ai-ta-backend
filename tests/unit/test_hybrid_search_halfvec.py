"""Regression guard: the semantic distance expression must compute in halfvec
against the STORED ``embedding_halfvec`` column, with no per-row cast (PR-4).

If this ever reverts to casting `embedding` per row, the query stops matching
document_chunks__embedding_halfvec_stored_hnsw__idx and the semantic scan goes
from ~0.1s back to ~0.6s+ (worse under RLS — see
docs/architecture/rag-pipeline/hybrid-search.md).
Compile-only — no database required.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from database.models import EMBEDDING_DIM, DocumentChunk
from retrieval.hybrid_search import (
    _build_keyword_cte,
    _build_semantic_cte,
    _halfvec_cosine_distance,
)

pytestmark = pytest.mark.unit


def _sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect())).lower()


def _compile_full(selectable) -> str:
    return str(select(selectable).compile(dialect=postgresql.dialect())).lower()


def test_query_operand_cast_chunk_side_is_plain_stored_column():
    expr = _halfvec_cosine_distance([0.1] * EMBEDDING_DIM)
    sql = _sql(expr)
    assert "embedding_halfvec" in sql, "chunk side must read the stored generated column"
    # Only the query vector (a Python list, not a stored row) needs casting —
    # the stored column is already halfvec, so there is exactly one cast.
    assert sql.count(f"halfvec({EMBEDDING_DIM})") == 1
    assert sql.count(f"extensions.halfvec({EMBEDDING_DIM})") == 1
    # The chunk column itself must not be wrapped in a per-row CAST.
    assert "embedding::" not in sql
    assert "cast(document_chunks.embedding as" not in sql
    assert "cast(document_chunks.embedding_halfvec as" not in sql


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
    cte = _build_semantic_cte([0.1] * EMBEDDING_DIM, [1, 2], n_results=300)
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


def test_semantic_cte_filters_by_doc_id_array_not_join():
    """The semantic candidate filter must be a chunk-local ``= ANY(array)`` over
    document_id, NOT a join to app.documents. Only the materialized-array form
    lets the HNSW index engage under hnsw.iterative_scan (verified on the live
    DB: a join or ``IN (subquery)`` reverts to a brute-force Sort over every
    embedding). If this regresses to a join, the iterative scan silently no-ops.
    """
    cte = _build_semantic_cte([0.1] * EMBEDDING_DIM, [1, 2], n_results=300)
    sql = _compile_full(cte)
    assert "app.documents" not in sql, (
        "semantic CTE must not join app.documents — that blocks the HNSW index"
    )
    assert "= any(" in sql, "expected chunk-local document_id = ANY(array) filter"
    assert "integer[]" in sql, "doc-id array must be a bound integer[] param"


def test_keyword_cte_limits_before_window():
    tsvector = func.to_tsvector("english", DocumentChunk.content)
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


def test_semantic_cte_still_uses_halfvec_stored_column():
    cte = _build_semantic_cte([0.1] * EMBEDDING_DIM, [], n_results=300)
    sql = _compile_full(cte)
    assert "embedding_halfvec" in sql
    # The distance expression is emitted once in SELECT and again in ORDER BY
    # (SQLAlchemy repeats it rather than referencing the label), so count
    # only the query-vector cast is present — not that it's exactly one.
    assert sql.count(f"halfvec({EMBEDDING_DIM})") >= 1
    assert "embedding::" not in sql, "chunk column must not be cast per row"
    assert "<=>" in sql
