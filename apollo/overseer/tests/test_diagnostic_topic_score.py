"""2026-07-10 topic-score design spec §4 — ``generate_diagnostic``'s prompt
selection.

Post flag-reset the ledger-grounded prompt is used UNCONDITIONALLY whenever a
``TopicScoreResult`` is supplied (the topic-score serving flag was deleted). No
``topic_score`` (or a soft-failed ``None``) -> the axis-based prompt, unchanged
from pre-topic-score behavior.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
            ),
        ),
    )


def _mock_client_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=text))]
    )
    return client


@patch("apollo.overseer.diagnostic.OpenAI")
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


@patch("apollo.overseer.diagnostic.OpenAI")
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


@patch("apollo.overseer.diagnostic.OpenAI")
def test_topic_score_uses_ledger_grounded_prompt(mock_client_cls):
    client = _mock_client_returning("ledger narrative")
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
    # 2026-07-11 feedback spec §2: canonical keys never reach the prompt —
    # the display name is the topic's only identity in the ledger text.
    assert "apply continuity" in user_msg["content"]
    assert "credit=" not in user_msg["content"]
    assert "weight=" not in user_msg["content"]
    # The axis-path payload shape must be gone.
    assert "reference_required_entries" not in user_msg["content"]


@patch("apollo.overseer.diagnostic.OpenAI")
def test_topic_score_still_appends_misconception_and_negotiation_lines(mock_client_cls):
    """The recap lines appended after the LLM call read rubric/coverage
    directly and are unaffected by which prompt generated the narrative."""
    client = _mock_client_returning("ledger narrative")
    mock_client_cls.return_value = client

    rubric_with_misc = dict(_RUBRIC)
    rubric_with_misc["misconception_corrected"] = {
        "score": 50,
        "letter": "F",
        "present": True,
        "detected": 1,
        "resolved": 0,
    }

    out = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=rubric_with_misc,
        topic_score=_topic_score(),
    )
    assert "suspected misconception" in out
