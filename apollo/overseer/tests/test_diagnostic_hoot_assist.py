"""INTERACTION5 — structured ``topic_feedback`` carries the ledger's
``hoot_assisted`` flag (Agent D).

The flag is code-injected from the ``TopicScoreResult`` in ``_parse_topic_feedback``,
NOT parsed from the LLM (which still returns only ``canonical_key``/``note``/
``quote``), so a Hoot-assisted topic is flagged deterministically regardless of
what the narrative prose says. Absent asides -> every item flags ``False``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit

_COVERAGE = {"per_step": {"p1": "partial", "p2": "missing"}, "procedure_scores": {}}
_RUBRIC = {
    "overall": {"score": 60, "letter": "C"},
    "procedure": {"score": 60, "letter": "C", "present": True},
    "justification": {"score": 80, "letter": "B", "present": True},
    "simplification": {"score": 80, "letter": "B", "present": True},
}


def _topic_score(*, p1_assisted: bool, p2_assisted: bool) -> TopicScoreResult:
    return TopicScoreResult(
        score=60,
        letter="C",
        coverage_component=0.6,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="p1",
                display_name="apply continuity",
                credit=0.5,
                status="partial",
                weight=0.5,
                misconceptions=(),
                evidence_span=None,
                hoot_assisted=p1_assisted,
            ),
            TopicCredit(
                canonical_key="p2",
                display_name="state assumptions",
                credit=0.0,
                status="missing",
                weight=0.5,
                misconceptions=(),
                evidence_span=None,
                hoot_assisted=p2_assisted,
            ),
        ),
    )


def _mock_client_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=text))]
    )
    return client


def _structured_reply() -> str:
    # The LLM emits ONLY the three declared fields — hoot_assisted is never in
    # the model output; the code injects it from the ledger.
    return json.dumps(
        {
            "headline": "Solid start.",
            "topic_feedback": [
                {"canonical_key": "p1", "note": "Make continuity explicit.", "quote": None},
                {"canonical_key": "p2", "note": "State the assumptions.", "quote": None},
            ],
            "next_step": "Connect the two states.",
        }
    )


@patch("apollo.overseer.diagnostic.bounded_client")
def test_topic_feedback_carries_hoot_assisted_flag_per_topic(mock_client_cls):
    mock_client_cls.return_value = _mock_client_returning(_structured_reply())

    _narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo.",
        rubric=_RUBRIC,
        topic_score=_topic_score(p1_assisted=True, p2_assisted=False),
    )

    assert feedback is not None
    by_key = {item["canonical_key"]: item for item in feedback["topic_feedback"]}
    assert by_key["p1"]["hoot_assisted"] is True
    assert by_key["p2"]["hoot_assisted"] is False


@patch("apollo.overseer.diagnostic.bounded_client")
def test_topic_feedback_flags_false_when_no_topic_assisted(mock_client_cls):
    mock_client_cls.return_value = _mock_client_returning(_structured_reply())

    _narrative, feedback = generate_diagnostic(
        coverage=_COVERAGE,
        reference_steps=[],
        problem_text="Demo.",
        rubric=_RUBRIC,
        topic_score=_topic_score(p1_assisted=False, p2_assisted=False),
    )

    assert feedback is not None
    assert all(item["hoot_assisted"] is False for item in feedback["topic_feedback"])
    # The flag is additive: the other declared fields are untouched.
    assert set(feedback["topic_feedback"][0]) == {
        "canonical_key",
        "note",
        "quote",
        "hoot_assisted",
    }
