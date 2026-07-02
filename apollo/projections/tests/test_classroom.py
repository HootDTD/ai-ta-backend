"""Campaign-plan Task B3 — pure/structural tests for
``apollo.projections.classroom``. Real-PG aggregation behavior (grouping,
windowing, JSONB lateral expansion, scoping) is covered by
``tests/database/test_classroom_projection_postgres.py``; router wiring is
covered by ``apollo/tests/test_classroom_routes.py``."""

from __future__ import annotations

import inspect

import pytest

from apollo.projections import classroom

pytestmark = pytest.mark.unit


def test_default_window_days_matches_spec_default():
    assert classroom.DEFAULT_WINDOW_DAYS == 14


def test_unresolved_status_excluded_from_reference_key_statuses():
    """``unresolved`` node-ledger rows key on a STUDENT node id (see
    ``apollo.grading.artifact_build._unresolved_ledger_entry``), not a
    reference canonical_key — including them in the coverage aggregate would
    surface opaque per-student ids instead of a reusable per-concept
    signal."""
    assert "unresolved" not in classroom._LEDGER_STATUSES_WITH_REFERENCE_KEY
    assert set(classroom._LEDGER_STATUSES_WITH_REFERENCE_KEY) == {"credited", "misconception"}


def test_mastery_heatmap_signature():
    sig = inspect.signature(classroom.mastery_heatmap)
    assert list(sig.parameters) == ["db", "search_space_id"]
    assert sig.parameters["search_space_id"].kind == inspect.Parameter.KEYWORD_ONLY


def test_struggle_signals_signature_has_windowed_default():
    sig = inspect.signature(classroom.struggle_signals)
    assert list(sig.parameters) == ["db", "search_space_id", "window_days"]
    assert sig.parameters["window_days"].default == classroom.DEFAULT_WINDOW_DAYS
