"""The canonical artifact's `misconceptions` array (P3.2 §2.5).

Hardwired `[]` until today. It is now DERIVED from the one `TopicScoreResult`
the Done produced, so the array and the served `topics[]` cannot disagree and
the level gate is inherited rather than re-implemented (only level >= 3 fills
`topics[].misconceptions`).

Three readers consume it and none of them changed: `projections/scorecard.
_watch_out` ({canonical_key, evidence_span}), `projections/classroom.
top_misconceptions` (`misc ->> 'canonical_key'`), and `persistence.
attempt_history.prior_wrongness_findings` (all three keys).
"""

from __future__ import annotations

import json

import pytest

from apollo.grading.artifact_build import build_llm_artifact
from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult
from apollo.projections.scorecard import render_scorecard

pytestmark = pytest.mark.unit

_COVERAGE = {"per_step": {"eq1": "covered", "c1": "missing"}, "confidences": {"eq1": 0.9}}
_RUBRIC = {"overall": {"score": 90, "letter": "A"}}
_SPAN = "Pressure rises wherever the flow speeds up."


def _topic(key: str, *, misconceptions: tuple[TopicMisconception, ...] = ()) -> TopicCredit:
    return TopicCredit(
        canonical_key=key,
        display_name=key,
        credit=1.0,
        status="covered",
        weight=0.5,
        misconceptions=misconceptions,
    )


def _result(*topics: TopicCredit, dock: float = 0.0) -> TopicScoreResult:
    return TopicScoreResult(
        score=90,
        letter="A",
        coverage_component=0.9,
        misconception_dock=dock,
        topics=tuple(topics),
    )


def _artifact(topic_score: TopicScoreResult | None) -> dict:
    return build_llm_artifact(
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        latency_ms=12,
        clarification_trace=[],
        topic_score=topic_score,
    )


def test_default_empty_is_byte_identical():
    """No topic score, and a topic score whose containers are empty (levels
    0-2), both reproduce the pre-P3.2 artifact exactly."""
    without = _artifact(None)
    empty_containers = _artifact(_result(_topic("eq1"), _topic("c1")))

    assert without["misconceptions"] == []
    assert empty_containers["misconceptions"] == []
    # The rest of the payload is untouched by the new derivation.
    assert json.dumps(
        {k: v for k, v in without.items() if k != "scores"}, sort_keys=True
    ) == json.dumps({k: v for k, v in empty_containers.items() if k != "scores"}, sort_keys=True)


def test_keys_match_scorecard_and_classroom_readers():
    artifact = _artifact(
        _result(
            _topic(
                "eq1",
                misconceptions=(
                    TopicMisconception(
                        canonical_key="eq1",
                        resolved=False,
                        dock_points=0.0,
                        evidence_span=_SPAN,
                    ),
                ),
            ),
            _topic("c1"),
        )
    )

    assert artifact["misconceptions"] == [
        {"canonical_key": "eq1", "resolved": False, "evidence_span": _SPAN}
    ]
    # `scorecard._watch_out` reads exactly these two keys — proved by running it.
    assert render_scorecard(artifact)["watch_out"] == [{"key": "eq1", "quote": _SPAN}]
    # `classroom.top_misconceptions` selects `misc ->> 'canonical_key'`, and
    # `attempt_history.prior_wrongness_findings` also reads `resolved`.
    assert set(artifact["misconceptions"][0]) == {"canonical_key", "resolved", "evidence_span"}


def test_the_array_is_always_a_json_array():
    """`grader_payload -> 'misconceptions'` is unrolled by a LATERAL guarded on
    `jsonb_typeof(...) = 'array'`; an object there silently yields zero rows."""
    for topic_score in (None, _result(), _result(_topic("eq1"))):
        assert isinstance(_artifact(topic_score)["misconceptions"], list)


def test_findings_from_several_topics_are_all_carried():
    artifact = _artifact(
        _result(
            _topic(
                "eq1",
                misconceptions=(
                    TopicMisconception(
                        canonical_key="eq1", resolved=False, dock_points=0.0, evidence_span=_SPAN
                    ),
                ),
            ),
            _topic(
                "c1",
                misconceptions=(
                    TopicMisconception(
                        canonical_key="c1", resolved=True, dock_points=0.0, evidence_span=None
                    ),
                ),
            ),
        )
    )

    assert [m["canonical_key"] for m in artifact["misconceptions"]] == ["eq1", "c1"]
    assert [m["resolved"] for m in artifact["misconceptions"]] == [False, True]
    # A missing span is None, not "": `prior_wrongness_findings` documents that
    # `evidence_span` may be null and the scorecard renders it as an empty quote.
    assert artifact["misconceptions"][1]["evidence_span"] is None
    assert render_scorecard(artifact)["watch_out"][1] == {"key": "c1", "quote": ""}
