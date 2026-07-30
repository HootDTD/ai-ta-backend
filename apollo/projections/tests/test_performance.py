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
    """The four axes written under ``diagnostic_report -> 'rubric'`` by
    ``apollo.overseer.rubric.compute_rubric`` — a rename there must be
    reconciled here or the loss signal silently reads NULLs."""
    assert performance._RUBRIC_DIMENSIONS == (
        "procedure",
        "justification",
        "simplification",
        "misconception_corrected",
    )


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
