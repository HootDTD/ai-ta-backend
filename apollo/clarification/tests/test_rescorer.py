"""Task 11 — unit tests for the clarification re-scorer (confirmed/refuted/vague judge).

No live model calls: rescore_clarification uses a DI'd judge stub in the brief's
tests; default_clarification_judge tests monkeypatch apollo.clarification.rescorer.main_chat.

2026-07-10 emergent misconception map plan (T3/Q2): ``rescore_clarification``
now returns a frozen ``RescoreResult{outcome, confidence}`` instead of the
bare ``RescoreOutcome`` literal, so the ``refuted`` capture seam can carry a
confidence weight. A literal-returning judge stub (every pre-existing test's
shape) still works via a back-compat shim (``confidence=1.0``); a judge that
returns a ``RescoreResult`` directly is passed through unchanged.
"""

import dataclasses

import pytest

from apollo.clarification.rescorer import (
    ClarificationRequest,
    RescoreResult,
    default_clarification_judge,
    rescore_clarification,
)

# ---------------------------------------------------------------------------
# Brief's tests (verbatim from task-11-brief.md) — literal-stub judges must
# keep working unchanged (R3 back-compat).
# ---------------------------------------------------------------------------


def _judge(outcome):
    def fn(request: ClarificationRequest):
        assert request.clarification_text  # judge sees the committed answer
        return outcome

    return fn


@pytest.mark.parametrize("outcome", ["confirmed", "refuted", "vague"])
def test_passes_through_three_way_verdict(outcome):
    got = rescore_clarification(
        original_statement="pressure and speed are related",
        clarification_text="pressure is lower where it moves faster",
        candidate_display="inverse pressure-velocity",
        judge=_judge(outcome),
    )
    assert isinstance(got, RescoreResult)
    assert got.outcome == outcome


@pytest.mark.parametrize("outcome", ["confirmed", "refuted", "vague"])
def test_literal_stub_judge_yields_confidence_one(outcome):
    """Back-compat shim (R3): a judge returning a bare literal (every
    pre-existing test stub's shape) is wrapped with confidence=1.0."""
    got = rescore_clarification(
        original_statement="o",
        clarification_text="c",
        candidate_display="d",
        judge=_judge(outcome),
    )
    assert got.confidence == 1.0


def test_judge_returning_rescore_result_passes_through_unchanged():
    """A judge that already returns a RescoreResult (the new native shape) is
    NOT re-wrapped — its confidence is preserved verbatim."""

    def judge(request):
        return RescoreResult(outcome="refuted", confidence=0.42)

    got = rescore_clarification(
        original_statement="o",
        clarification_text="c",
        candidate_display="d",
        judge=judge,
    )
    assert got == RescoreResult(outcome="refuted", confidence=0.42)


def test_judge_failure_propagates_named_error():
    from apollo.errors import ResolutionUnavailableError

    def boom(request):
        raise ResolutionUnavailableError(stage="clarification_rescore", last_error="503")

    with pytest.raises(ResolutionUnavailableError):
        rescore_clarification(
            original_statement="o",
            clarification_text="c",
            candidate_display="d",
            judge=boom,
        )


def test_rescore_result_is_frozen():
    result = RescoreResult(outcome="confirmed", confidence=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.confidence = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Additional tests for 100% coverage of default_clarification_judge + _build_messages
# ---------------------------------------------------------------------------


def _request():
    return ClarificationRequest(
        original_statement="pressure and speed are related",
        clarification_text="pressure is lower where it moves faster",
        candidate_display="inverse pressure-velocity",
    )


def test_default_judge_parses_confirmed(monkeypatch):
    monkeypatch.setattr(
        "apollo.clarification.rescorer.main_chat",
        lambda **kwargs: '{"verdict": "confirmed", "confidence": 0.9}',
    )
    result = default_clarification_judge(_request())
    assert result == RescoreResult(outcome="confirmed", confidence=0.9)


def test_default_judge_unknown_verdict_defaults_to_vague(monkeypatch):
    monkeypatch.setattr(
        "apollo.clarification.rescorer.main_chat",
        lambda **kwargs: '{"verdict": "banana", "confidence": 0.9}',
    )
    result = default_clarification_judge(_request())
    assert result.outcome == "vague"
    assert result.confidence == 0.0  # unknown verdict -> no-weight, per Q2


def test_default_judge_missing_confidence_defaults_to_zero(monkeypatch):
    monkeypatch.setattr(
        "apollo.clarification.rescorer.main_chat",
        lambda **kwargs: '{"verdict": "refuted"}',
    )
    result = default_clarification_judge(_request())
    assert result == RescoreResult(outcome="refuted", confidence=0.0)


def test_default_judge_non_numeric_confidence_defaults_to_zero(monkeypatch):
    monkeypatch.setattr(
        "apollo.clarification.rescorer.main_chat",
        lambda **kwargs: '{"verdict": "refuted", "confidence": "high"}',
    )
    result = default_clarification_judge(_request())
    assert result == RescoreResult(outcome="refuted", confidence=0.0)


def test_default_judge_wraps_infra_failure(monkeypatch):
    from apollo.errors import ResolutionUnavailableError

    def boom(**kwargs):
        raise RuntimeError("503 upstream")

    monkeypatch.setattr("apollo.clarification.rescorer.main_chat", boom)
    with pytest.raises(ResolutionUnavailableError) as ei:
        default_clarification_judge(_request())
    assert ei.value.stage == "clarification_rescore"


def test_default_judge_reraises_resolution_unavailable(monkeypatch):
    from apollo.errors import ResolutionUnavailableError

    def boom(**kwargs):
        raise ResolutionUnavailableError(stage="llm_adjudication", last_error="x")

    monkeypatch.setattr("apollo.clarification.rescorer.main_chat", boom)
    with pytest.raises(ResolutionUnavailableError) as ei:
        default_clarification_judge(_request())
    assert (
        ei.value.stage == "llm_adjudication"
    )  # original error re-raised unchanged, not re-wrapped
