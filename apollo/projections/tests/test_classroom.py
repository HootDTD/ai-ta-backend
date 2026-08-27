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
    assert set(classroom._LEDGER_STATUSES_WITH_REFERENCE_KEY) == {
        "credited",
        "misconception",
        "unprobed",
    }


def test_unprobed_rows_stay_in_the_lowest_coverage_signal():
    """Review fix. Before P1.2b a graded node nobody ever raised was filed
    ``unresolved`` with a NULL evidence span and surfaced as a 0.0-coverage
    worst offender — "this concept is never being taught or asked about" is
    exactly the signal that branch exists for. P1.2b moved those rows to their
    own status, which silently deleted the signal; matching it explicitly keeps
    them counted, and the status constant is imported so the two modules cannot
    drift."""
    assert classroom.LEDGER_STATUS_UNPROBED == "unprobed"
    assert classroom.LEDGER_STATUS_UNPROBED in classroom._LEDGER_STATUSES_WITH_REFERENCE_KEY


def test_mastery_heatmap_signature():
    sig = inspect.signature(classroom.mastery_heatmap)
    assert list(sig.parameters) == ["db", "search_space_id"]
    assert sig.parameters["search_space_id"].kind == inspect.Parameter.KEYWORD_ONLY


def test_struggle_signals_signature_has_windowed_default():
    sig = inspect.signature(classroom.struggle_signals)
    assert list(sig.parameters) == ["db", "search_space_id", "window_days"]
    assert sig.parameters["window_days"].default == classroom.DEFAULT_WINDOW_DAYS
