"""Pure/structural tests for ``apollo.projections.performance``. Real-PG
aggregation behavior (best-wins, bucketing, joins, identity degradation) is
covered by ``tests/database/test_class_performance_postgres.py``; router
wiring by ``apollo/tests/test_class_performance_routes.py``."""

from __future__ import annotations

import inspect

import pytest

from apollo.overseer.rubric import LETTER_BANDS
from apollo.projections import performance

pytestmark = pytest.mark.unit


def test_class_performance_signature():
    sig = inspect.signature(performance.class_performance)
    assert list(sig.parameters) == ["db", "search_space_id"]
    assert sig.parameters["search_space_id"].kind == inspect.Parameter.KEYWORD_ONLY


def test_rubric_dimensions_match_persisted_rubric_axes():
    """The axes written under ``diagnostic_report -> 'rubric'`` by
    ``apollo.overseer.rubric.compute_rubric`` that v2 surfaces — a rename there
    must be reconciled here or the loss signal silently reads NULLs. v2 drops the
    grader's fourth axis (``misconception_corrected``) as noise."""
    assert performance._RUBRIC_DIMENSIONS == (
        "procedure",
        "justification",
        "simplification",
    )
    assert "misconception_corrected" not in performance._RUBRIC_DIMENSIONS


def test_problems_block_groups_orders_and_zero_fills():
    """Pure best-wins problem rollup: per-problem letter distribution over every
    band (zeros included), distinct graded students, and concept_name/problem_code
    ordering."""
    best_rows = [
        {"user_id": "u1", "problem_id": 1, "score": 85.0, "letter": "A-"},
        {"user_id": "u2", "problem_id": 1, "score": 100.0, "letter": "A+"},
        {"user_id": "u1", "problem_id": 2, "score": 55.0, "letter": "D"},
    ]
    meta = {
        1: {"problem_code": "pb", "concept_id": 7, "concept_name": "Beta"},
        2: {"problem_code": "pa", "concept_id": 5, "concept_name": "Alpha"},
    }
    block = performance._problems_block(best_rows, meta)
    # ordered by concept_name then problem_code: Alpha/pa (problem 2) before Beta/pb.
    assert [p["problem_id"] for p in block] == [2, 1]
    alpha, beta = block
    assert (alpha["concept_name"], alpha["problem_code"]) == ("Alpha", "pa")
    assert (beta["students_graded"], beta["avg_best"]) == (2, 92.5)  # (85+100)/2
    # distribution covers every band, zeros included, verbatim served letters.
    beta_dist = {row["letter"]: row["count"] for row in beta["distribution"]}
    assert beta_dist["A-"] == 1 and beta_dist["A+"] == 1 and beta_dist["F"] == 0
    assert [row["letter"] for row in beta["distribution"]] == [
        letter for _t, letter in LETTER_BANDS
    ]


def test_problems_block_empty():
    assert performance._problems_block([], {}) == []


def test_distribution_presents_every_grader_band_in_order():
    rows = performance._distribution([])
    assert [row["letter"] for row in rows] == [letter for _t, letter in LETTER_BANDS]
    assert all(row["count"] == 0 for row in rows)


def test_distribution_counts_served_letters_verbatim():
    rows = performance._distribution(
        [
            {"user_id": "u1", "problem_id": 1, "score": 85.0, "letter": "A-"},
            {"user_id": "u1", "problem_id": 2, "score": 100.0, "letter": "A+"},
            {"user_id": "u2", "problem_id": 1, "score": 84.0, "letter": "B+"},
        ]
    )
    counts = {row["letter"]: row["count"] for row in rows}
    assert counts["A-"] == 1 and counts["A+"] == 1 and counts["B+"] == 1
    assert sum(counts.values()) == 3
