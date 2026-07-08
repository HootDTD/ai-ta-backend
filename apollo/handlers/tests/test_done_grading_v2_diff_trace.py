"""Tests for the optional clarification-v2 incremental/batch diff trace
(integration spec §7, task T13). Default OFF, observability only -- never
touches the grade."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from apollo.handlers import done_grading as dg


def test_diff_trace_flag_default_off(monkeypatch):
    monkeypatch.delenv("APOLLO_CLARIFICATION_V2_DIFF_TRACE", raising=False)
    assert dg._clarification_v2_diff_trace_enabled() is False


def test_diff_trace_flag_truthy_values(monkeypatch):
    for value in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("APOLLO_CLARIFICATION_V2_DIFF_TRACE", value)
        assert dg._clarification_v2_diff_trace_enabled() is True
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_DIFF_TRACE", "nonsense")
    assert dg._clarification_v2_diff_trace_enabled() is False


def test_diff_trace_noop_when_no_live_snapshot(monkeypatch, caplog):
    """No snapshot for this attempt (cold worker / turn 1) -> silent no-op,
    never logs, never raises."""
    from apollo.handlers.chat import _INCREMENTAL_HOLDER

    monkeypatch.setattr(_INCREMENTAL_HOLDER, "latest_snapshot", lambda attempt_id: None)

    caplog.set_level(logging.INFO)
    grade = SimpleNamespace(node_coverage_score=0.7)
    dg._log_clarification_v2_diff_trace(attempt_id=123, grade=grade, resolver_v2_trace=None)

    assert "clarification_v2_diff_trace" not in caplog.text


def test_diff_trace_logs_aggregate_delta_without_per_node_trace(monkeypatch, caplog):
    from apollo.handlers.chat import _INCREMENTAL_HOLDER

    snapshot = SimpleNamespace(node_cov=0.6, node_credits={"cond.a": 0.6})
    monkeypatch.setattr(_INCREMENTAL_HOLDER, "latest_snapshot", lambda attempt_id: snapshot)

    caplog.set_level(logging.INFO)
    grade = SimpleNamespace(node_coverage_score=0.8)
    dg._log_clarification_v2_diff_trace(attempt_id=7, grade=grade, resolver_v2_trace=None)

    assert "clarification_v2_diff_trace attempt_id=7" in caplog.text
    assert "incremental_node_cov=0.6000" in caplog.text
    assert "batch_node_cov=0.8000" in caplog.text
    assert "max_abs_node_delta=0.2000" in caplog.text


def test_diff_trace_uses_per_node_max_when_trace_available(monkeypatch, caplog):
    from apollo.handlers.chat import _INCREMENTAL_HOLDER

    snapshot = SimpleNamespace(
        node_cov=0.5, node_credits={"cond.a": 0.2, "cond.b": 0.9}
    )
    monkeypatch.setattr(_INCREMENTAL_HOLDER, "latest_snapshot", lambda attempt_id: snapshot)

    caplog.set_level(logging.INFO)
    grade = SimpleNamespace(node_coverage_score=0.5)
    resolver_v2_trace = {
        "nodes": [
            {"canonical_key": "cond.a", "credit": 0.2},
            {"canonical_key": "cond.b", "credit": 0.3},  # 0.6 abs delta -- the max
        ]
    }
    dg._log_clarification_v2_diff_trace(
        attempt_id=8, grade=grade, resolver_v2_trace=resolver_v2_trace
    )

    assert "max_abs_node_delta=0.6000" in caplog.text


def test_diff_trace_never_raises_and_never_touches_grade(monkeypatch, caplog):
    """Any failure inside is swallowed -- pure observability."""

    def boom(attempt_id):
        raise RuntimeError("holder blew up")

    from apollo.handlers.chat import _INCREMENTAL_HOLDER

    monkeypatch.setattr(_INCREMENTAL_HOLDER, "latest_snapshot", boom)

    caplog.set_level(logging.WARNING)
    grade = SimpleNamespace(node_coverage_score=0.5)
    # Must not raise.
    dg._log_clarification_v2_diff_trace(attempt_id=9, grade=grade, resolver_v2_trace=None)
    assert "clarification_v2_diff_trace_failed attempt_id=9" in caplog.text
