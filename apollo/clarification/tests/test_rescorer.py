"""Task 11 — unit tests for the clarification re-scorer (confirmed/refuted/vague judge).

No live model calls: rescore_clarification uses a DI'd judge stub in the brief's
tests; default_clarification_judge tests monkeypatch apollo.clarification.rescorer.main_chat.
"""

import pytest

from apollo.clarification.rescorer import (
    ClarificationRequest,
    default_clarification_judge,
    rescore_clarification,
)

# ---------------------------------------------------------------------------
# Brief's tests (verbatim from task-11-brief.md)
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
    assert got == outcome


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
        lambda **kwargs: '{"verdict": "confirmed"}',
    )
    assert default_clarification_judge(_request()) == "confirmed"


def test_default_judge_unknown_verdict_defaults_to_vague(monkeypatch):
    monkeypatch.setattr(
        "apollo.clarification.rescorer.main_chat",
        lambda **kwargs: '{"verdict": "banana"}',
    )
    assert default_clarification_judge(_request()) == "vague"


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
