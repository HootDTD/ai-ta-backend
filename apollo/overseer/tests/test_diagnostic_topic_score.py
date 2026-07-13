"""2026-07-10 topic-score design spec §3/§4 — ``generate_diagnostic``'s
flag-gated switch to the ledger-grounded prompt.

Flag OFF (default) -> byte-identical to pre-topic-score behavior, even when a
``topic_score`` is passed in (the caller in ``done.py`` always computes it
flag-independently, so this module must ignore it while the serving flag is
off). Flag ON + a topic_score -> the ledger-grounded prompt REPLACES the
axis-based one.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

_FLAG = "APOLLO_TOPIC_SCORE_SERVED"

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


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    yield


@patch("apollo.overseer.diagnostic.OpenAI")
def test_flag_off_topic_score_argument_is_ignored_axis_prompt_used(mock_client_cls, monkeypatch):
    """Even when a topic_score is passed (done.py always computes one), the
    flag being OFF means the axis-based prompt is used byte-identically —
    the caller is NEVER required to omit the argument for safety."""
    monkeypatch.delenv(_FLAG, raising=False)
    client = _mock_client_returning("axis narrative")
    mock_client_cls.return_value = client

    out_with_topic_score = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
        topic_score=_topic_score(),
    )
    called_with = client.chat.completions.create.call_args

    client2 = _mock_client_returning("axis narrative")
    mock_client_cls.return_value = client2
    out_without_topic_score = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo problem.",
        rubric=_RUBRIC,
    )
    called_without = client2.chat.completions.create.call_args

    assert called_with.kwargs["messages"] == called_without.kwargs["messages"]
    assert out_with_topic_score == out_without_topic_score


@patch("apollo.overseer.diagnostic.OpenAI")
def test_flag_off_no_topic_score_argument_byte_identical_to_pre_topic_score(
    mock_client_cls, monkeypatch
):
    monkeypatch.delenv(_FLAG, raising=False)
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
def test_flag_on_no_topic_score_falls_back_to_axis_prompt(mock_client_cls, monkeypatch):
    """Flag ON but topic_score is None (compute_topic_score soft-failed) ->
    the axis-based prompt is used (never crash on a missing ledger)."""
    monkeypatch.setenv(_FLAG, "true")
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
def test_flag_on_with_topic_score_uses_ledger_grounded_prompt(mock_client_cls, monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
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
def test_flag_on_with_topic_score_still_appends_misconception_and_negotiation_lines(
    mock_client_cls,
    monkeypatch,
):
    """The recap lines appended after the LLM call read rubric/coverage
    directly and are unaffected by which prompt generated the narrative."""
    monkeypatch.setenv(_FLAG, "true")
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
