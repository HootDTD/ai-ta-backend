"""Regression guard: the semantic distance expression must compute in halfvec.

If this ever reverts to raw `vector` distance, the query stops matching
idx_aita_chunks_embedding_hnsw and the semantic scan goes from ~0.1s back to ~3.5s.
Compile-only — no database required.
"""
from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from database.models import EMBEDDING_DIM
from retrieval.hybrid_search import _halfvec_cosine_distance

pytestmark = pytest.mark.unit


def _sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect())).lower()


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
