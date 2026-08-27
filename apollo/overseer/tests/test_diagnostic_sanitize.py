"""The diagnostic return boundary removes scoring internals."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit

_RUBRIC = {
    "overall": {"score": 64, "letter": "C"},
    "procedure": {"score": 64, "letter": "C", "present": True},
    "justification": {"score": 0, "letter": "F", "present": False},
    "simplification": {"score": 0, "letter": "F", "present": False},
}


def _topic_score() -> TopicScoreResult:
    return TopicScoreResult(
        score=64,
        letter="C",
        coverage_component=0.64,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="proc_explain_causality",
                display_name="Explain causality",
                credit=0.9,
                status="covered",
                weight=0.23,
                misconceptions=(),
            ),
        ),
    )


def _client_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content=text))
    ]
    return client


@patch("apollo.overseer.diagnostic.bounded_client")
def test_topic_output_is_sanitized(mock_openai):
    mock_openai.return_value = _client_returning(
        "Great work (proc_explain_causality, credit 0.90, weight 0.23). Misconception dock: 0.000."
    )

    out, feedback = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert out == "Great work."
    assert feedback is None


@patch("apollo.overseer.diagnostic.bounded_client")
def test_legacy_output_is_pattern_sanitized(mock_openai):
    mock_openai.return_value = _client_returning("Good start, credit=0.80 overall.")

    out, feedback = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
    )

    assert out == "Good start, overall."
    assert feedback is None


@patch("apollo.overseer.diagnostic.bounded_client")
def test_clean_output_and_placeholder_are_preserved(mock_openai):
    # Study-prep 2026-08-23: this fixture used to read "…well (80%)." and pinned
    # the percentage as PRESERVED. Students now see a proficiency band and never
    # a number, so the percentage is no longer clean text — it is scrubbed
    # (`test_percentage_leak_is_scrubbed_end_to_end` below). What this test still
    # pins is the untouched-input contract: prose with nothing to remove comes
    # back byte-identical, paragraph breaks and all.
    clean = "You covered causality well and grounded it in the pipe example.\n\nNext step: explain overload."
    mock_openai.return_value = _client_returning(clean)
    kwargs = {
        "coverage": {"per_step": {}, "procedure_scores": {}},
        "reference_steps": [],
        "problem_text": "P?",
        "rubric": _RUBRIC,
        "topic_score": _topic_score(),
    }
    assert generate_diagnostic(**kwargs) == (clean, None)

    mock_openai.return_value.chat.completions.create.side_effect = RuntimeError("boom")
    assert generate_diagnostic(**kwargs) == (
        "[Diagnostic narrative unavailable — the grade above is still accurate.]",
        None,
    )


@patch("apollo.overseer.diagnostic.bounded_client")
def test_percentage_leak_is_scrubbed_end_to_end(mock_openai):
    """Study-prep 2026-08-23: a percentage reaching the return boundary is a
    numeric grade the student is not supposed to see, so the whole serving lane
    — not just the unit-tested regex — has to drop it."""
    mock_openai.return_value = _client_returning(
        "You covered causality well (80%).\n\nNext step: explain overload."
    )

    out, feedback = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert out == "You covered causality well.\n\nNext step: explain overload."
    assert feedback is None


@patch("apollo.overseer.diagnostic.bounded_client")
def test_structured_text_fields_are_sanitized_individually(mock_openai):
    topic_score = _topic_score()
    topic_score = TopicScoreResult(
        score=topic_score.score,
        letter=topic_score.letter,
        coverage_component=topic_score.coverage_component,
        misconception_dock=topic_score.misconception_dock,
        topics=(
            TopicCredit(
                canonical_key="proc_explain_causality",
                display_name="Explain causality",
                credit=0.9,
                status="covered",
                weight=0.23,
                misconceptions=(),
                evidence_span="You linked cause and effect.",
            ),
        ),
    )
    mock_openai.return_value = _client_returning(
        json.dumps(
            {
                "headline": "Strong proc_explain_causality start (credit 0.90).",
                "topic_feedback": [
                    {
                        "canonical_key": "proc_explain_causality",
                        "note": "Keep the causal link (weight 0.23).",
                        "quote": "You linked cause and effect.",
                    }
                ],
                "next_step": "Explain the effect (dock 0.10).",
            }
        )
    )

    narrative, feedback = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="P?",
        rubric=_RUBRIC,
        topic_score=topic_score,
    )

    assert feedback == {
        "headline": "Strong start.",
        "topic_feedback": [
            {
                "canonical_key": "proc_explain_causality",
                "note": "Keep the causal link.",
                "quote": "You linked cause and effect.",
                "hoot_assisted": False,
            }
        ],
        "recap": [],
        "next_step": "Explain the effect.",
    }
    assert narrative == ("Strong start.\n\nKeep the causal link.\n\nNext step: Explain the effect.")
