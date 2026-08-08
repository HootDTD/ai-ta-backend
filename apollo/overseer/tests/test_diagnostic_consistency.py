"""P2.1 (2026-08-07 bimodal-fix spec §5) — ``generate_diagnostic`` serves a
narrative that cannot contradict the per-node verdicts.

The consistency gate runs INSIDE the structured-feedback path, after
sanitization and before flattening, so both the served ``feedback`` block and
the back-compat flattened narrative carry the repaired prose. Mirrors the
mocked-``bounded_client`` harness of ``test_diagnostic_topic_score.py`` — no
live LLM call.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit

_COVERAGE: dict[str, Any] = {"per_step": {"p1": "missing"}, "procedure_scores": {}}
_RUBRIC: dict[str, Any] = {"overall": {"score": 28, "letter": "F"}}


def _topic_score(credit: float) -> TopicScoreResult:
    return TopicScoreResult(
        score=28,
        letter="F",
        coverage_component=credit,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="p1",
                display_name="contrast democratization and centralization",
                credit=credit,
                status="missing" if credit <= 0.0 else "covered",
                weight=1.0,
                misconceptions=(),
            ),
        ),
    )


def _client(payload: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))]
    )
    return client


_PRAISE_PAYLOAD: dict[str, Any] = {
    "headline": "You clearly contrasted democratization and centralization.",
    "topic_feedback": [
        {
            "canonical_key": "p1",
            "note": "You clearly contrasted democratization and centralization with examples.",
            "quote": None,
        }
    ],
    "next_step": "Keep going.",
}


def _run(credit: float, client: MagicMock) -> tuple[str, dict[str, Any] | None]:
    with patch("apollo.overseer.diagnostic.bounded_client", return_value=client):
        return generate_diagnostic(
            coverage=_COVERAGE,
            reference_steps=[],
            problem_text="Demo problem.",
            rubric=_RUBRIC,
            topic_score=_topic_score(credit),
        )


def test_zeroed_topic_never_serves_praise_and_names_its_gap() -> None:
    narrative, feedback = _run(0.0, _client(_PRAISE_PAYLOAD))

    assert feedback is not None
    note = feedback["topic_feedback"][0]["note"]
    assert "clearly contrasted" not in note
    assert "contrast democratization and centralization" in note
    assert "clearly contrasted" not in feedback["headline"]
    # The flattened back-compat narrative carries the repaired prose too.
    assert "clearly contrasted" not in narrative
    assert "contrast democratization and centralization" in narrative


def test_credited_topic_keeps_the_model_prose_verbatim() -> None:
    _narrative, feedback = _run(1.0, _client(_PRAISE_PAYLOAD))

    assert feedback is not None
    assert feedback["headline"] == _PRAISE_PAYLOAD["headline"]
    assert feedback["topic_feedback"][0]["note"] == _PRAISE_PAYLOAD["topic_feedback"][0]["note"]
    assert feedback["next_step"] == _PRAISE_PAYLOAD["next_step"]


def test_gate_failure_never_escapes_into_grading() -> None:
    """A defect in the gate soft-fails the whole structured path, never raises."""
    with patch(
        "apollo.overseer.diagnostic.enforce_narrative_consistency",
        side_effect=RuntimeError("gate exploded"),
    ):
        narrative, feedback = _run(0.0, _client(_PRAISE_PAYLOAD))

    assert isinstance(narrative, str)
    assert feedback is None
