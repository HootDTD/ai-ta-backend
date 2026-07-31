"""Pure/structural tests for ``apollo.projections.performance``. Real-PG
aggregation behavior (best-wins, bucketing, joins, identity degradation) is
covered by ``tests/database/test_class_performance_postgres.py``; router
wiring by ``apollo/tests/test_class_performance_routes.py``; the per-problem
``problems[]`` block (distribution, students, node breakdown) by
``apollo/projections/tests/test_performance_problems.py``."""

from __future__ import annotations

import inspect

import pytest

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
