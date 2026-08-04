"""2026-07-10 topic-score design spec §4 — ``generate_diagnostic``'s prompt
selection.

Post flag-reset the ledger-grounded prompt is used UNCONDITIONALLY whenever a
``TopicScoreResult`` is supplied (the topic-score serving flag was deleted). No
``topic_score`` (or a soft-failed ``None``) -> the axis-based prompt, unchanged
from pre-topic-score behavior.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

_COVERAGE = {"per_step": {"p1": "missing"}, "procedure_scores": {}}
_RUBRIC = {
    "overall": {"score": 78, "letter": "B+"},
    "procedure": {"score": 60, "letter": "C+", "present": True},
    "justification": {"score": 100, "letter": "A", "present": True},
    "simplification": {"score": 100, "letter": "A", "present": True},
}


def _topic_score() -> TopicScoreResult:
    return TopicScoreResult(
        score=78,
        letter="B+",
        coverage_component=0.8,
        misconception_dock=0.02,
        topics=(
            TopicCredit(
                canonical_key="p1",
                display_name="apply continuity",
                credit=0.0,
                status="missing",
                weight=1.0,
                misconceptions=(),
                evidence_span="I applied continuity.",
            ),
        ),
    )


def _mock_client_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=text))]
    )
    return client


def _structured_reply(*, quote: str | None = "I applied continuity.") -> str:
    return json.dumps(
        {
            "headline": "You identified the main method.",
            "topic_feedback": [
                {
                    "canonical_key": "p1",
                    "note": "Make the continuity step explicit.",
                    "quote": quote,
                }
            ],
            "next_step": "Show how continuity connects the two states.",
        }
    )


@patch("apollo.overseer.diagnostic.bounded_client")
def test_no_topic_score_uses_axis_prompt(mock_client_cls):
    client = _mock_client_returning("axis narrative")
    mock_client_cls.return_value = client

    generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[{"id": "p1", "entry_type": "procedure_step", "content": {"action": "x"}}],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
    )
    called = client.chat.completions.create.call_args
    user_msg = next(m for m in called.kwargs["messages"] if m["role"] == "user")
    # The axis-path user payload is the JSON dict shape (rubric/coverage keys).
    assert "reference_required_entries" in user_msg["content"]


@patch("apollo.overseer.diagnostic.bounded_client")
def test_none_topic_score_falls_back_to_axis_prompt(mock_client_cls):
    """``topic_score=None`` (compute_topic_score soft-failed) -> the axis-based
    prompt is used (never crash on a missing ledger)."""
    client = _mock_client_returning("axis narrative")
    mock_client_cls.return_value = client

    generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=None,
    )
    called = client.chat.completions.create.call_args
    user_msg = next(m for m in called.kwargs["messages"] if m["role"] == "user")
    assert "reference_required_entries" in user_msg["content"]


@patch("apollo.overseer.diagnostic.bounded_client")
def test_topic_score_uses_ledger_grounded_prompt(mock_client_cls):
    client = _mock_client_returning(_structured_reply())
    mock_client_cls.return_value = client

    generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )
    called = client.chat.completions.create.call_args
    system_msg = next(m for m in called.kwargs["messages"] if m["role"] == "system")
    user_msg = next(m for m in called.kwargs["messages"] if m["role"] == "user")

    assert "ledger" in system_msg["content"].lower()
    assert 'canonical_key="p1"' in user_msg["content"]
    assert "apply continuity" in user_msg["content"]
    assert "credit=" not in user_msg["content"]
    assert "weight=" not in user_msg["content"]
    # The axis-path payload shape must be gone.
    assert "reference_required_entries" not in user_msg["content"]
    assert called.kwargs["response_format"]["type"] == "json_schema"
    assert called.kwargs["response_format"]["json_schema"]["strict"] is True


@patch("apollo.overseer.diagnostic.bounded_client")
def test_structured_success_returns_feedback_and_flattened_narrative(mock_client_cls):
    client = _mock_client_returning(_structured_reply())
    mock_client_cls.return_value = client

    narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert feedback == {
        "headline": "You identified the main method.",
        "topic_feedback": [
            {
                "canonical_key": "p1",
                "note": "Make the continuity step explicit.",
                "quote": "I applied continuity.",
                "hoot_assisted": False,
            }
        ],
        "recap": [],
        "next_step": "Show how continuity connects the two states.",
    }
    assert narrative == (
        "You identified the main method.\n\n"
        "Make the continuity step explicit.\n\n"
        "Next step: Show how continuity connects the two states."
    )


@patch("apollo.overseer.diagnostic.bounded_client")
def test_json_parse_failure_soft_fails_to_legacy_narrative(mock_client_cls):
    client = _mock_client_returning("legacy ledger narrative")
    mock_client_cls.return_value = client

    narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert narrative == "legacy ledger narrative"
    assert feedback is None
    assert client.chat.completions.create.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"headline": "Headline", "topic_feedback": [], "next_step": "Next", "extra": True},
        {"headline": 1, "topic_feedback": [], "next_step": "Next"},
        {"headline": "Headline", "topic_feedback": {}, "next_step": "Next"},
        {
            "headline": "Headline",
            "topic_feedback": [{"canonical_key": "p1", "note": "Note"}],
            "next_step": "Next",
        },
        {
            "headline": "Headline",
            "topic_feedback": [{"canonical_key": "unknown", "note": "Note", "quote": None}],
            "next_step": "Next",
        },
        {
            "headline": "Headline",
            "topic_feedback": [{"canonical_key": "p1", "note": "Note", "quote": 1}],
            "next_step": "Next",
        },
        {"headline": "Headline", "topic_feedback": [], "next_step": "Next"},
    ],
)
@patch("apollo.overseer.diagnostic.bounded_client")
def test_json_validation_failure_soft_fails_without_a_second_completion(
    mock_client_cls,
    payload,
):
    raw = json.dumps(payload)
    client = _mock_client_returning(raw)
    mock_client_cls.return_value = client

    narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert isinstance(narrative, str)
    assert feedback is None
    assert client.chat.completions.create.call_count == 1


@patch(
    "apollo.overseer.diagnostic.build_topic_narrative_prompt",
    side_effect=RuntimeError("prompt failure"),
)
def test_unexpected_topic_feedback_failure_never_escapes(_mock_prompt):
    narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert narrative == "[Diagnostic narrative unavailable — the grade above is still accurate.]"
    assert feedback is None


@patch("apollo.overseer.diagnostic.bounded_client")
def test_quote_mismatch_is_nulled_without_rejecting_feedback(mock_client_cls):
    client = _mock_client_returning(_structured_reply(quote="I invented this quote."))
    mock_client_cls.return_value = client

    _narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )

    assert feedback is not None
    assert feedback["topic_feedback"][0]["quote"] is None


@patch("apollo.overseer.diagnostic.bounded_client")
def test_topic_score_recap_lines_are_deterministic_and_code_appended(mock_client_cls):
    """The model shape has no recap; code appends current helper outputs."""
    client = _mock_client_returning(_structured_reply())
    mock_client_cls.return_value = client

    rubric_with_misc = dict(_RUBRIC)
    rubric_with_misc["misconception_corrected"] = {
        "score": 50,
        "letter": "F",
        "present": True,
        "detected": 1,
        "resolved": 0,
    }

    coverage_with_negotiation = {
        **_COVERAGE,
        "negotiation_counts": {"paraphrased": 1, "skipped": 0, "disputed": 1},
    }

    narrative, feedback = generate_diagnostic(
        coverage=coverage_with_negotiation,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=rubric_with_misc,
        topic_score=_topic_score(),
    )
    assert feedback is not None
    assert feedback["recap"] == [
        (
            "During the conversation, you and Apollo navigated through "
            "1 suspected misconception; you resolved 0 of them."
        ),
        "You negotiated 2 entries with Apollo: 1 paraphrased, 1 disputed.",
    ]
    assert narrative.endswith(
        "During the conversation, you and Apollo navigated through "
        "1 suspected misconception; you resolved 0 of them.\n\n"
        "You negotiated 2 entries with Apollo: 1 paraphrased, 1 disputed.\n\n"
        "Next step: Show how continuity connects the two states."
    )
    raw_payload = json.loads(client.chat.completions.create.return_value.choices[0].message.content)
    assert "recap" not in raw_payload
