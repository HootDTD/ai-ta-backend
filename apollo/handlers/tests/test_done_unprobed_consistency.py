"""One Done payload must not say "not part of this grade" and "you missed
this" about the same topic (2026-08-07 bimodal-fix, defect U2).

P1.2b gives a graded node the tutor never raised ``weight 0`` / status
``unprobed``. It still carries credit 0, so every consumer that enumerates
topics as gaps would name it:

* the narrative prompt (``topic_narrative`` joins ``result.topics`` with no
  weight filter) and the structured topic feedback keyed off the same list;
* the canonical artifact's ``node_ledger``, whose ``unresolved`` rows render
  into ``student_response["scorecard"].missing_or_unclear`` as
  "Next time, explain X".

``done.py`` therefore narrates a GRADED-ONLY view (``graded_topics_only``) while
serving the FULL ``topics[]``, and ``build_llm_artifact`` files unprobed nodes
under their own ledger status.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apollo.grading.artifact_build import LEDGER_STATUS_UNPROBED, build_llm_artifact
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult
from apollo.projections.scorecard import render_scorecard

pytestmark = pytest.mark.unit


def _topic(key: str, *, status: str, weight: float, credit: float) -> TopicCredit:
    return TopicCredit(
        canonical_key=key,
        display_name=key,
        credit=credit,
        status=status,  # type: ignore[arg-type]
        weight=weight,
        misconceptions=(),
    )


def _result() -> TopicScoreResult:
    return TopicScoreResult(
        score=100,
        letter="A+",
        coverage_component=1.0,
        misconception_dock=0.0,
        topics=(
            _topic("eq.taught", status="covered", weight=1.0, credit=1.0),
            _topic("eq.never", status="unprobed", weight=0.0, credit=0.0),
        ),
    )


def _drop(patches, *attributes):
    return [p for p in patches if getattr(p, "attribute", None) not in attributes]


async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
    db, _sess, _attempt, patches = _old_path_patches()
    patches = _drop(patches, "compute_topic_score")
    patches.append(
        patch("apollo.handlers.done.compute_topic_score", new=MagicMock(return_value=_result()))
    )

    from apollo.handlers.done import handle_done

    started: dict[str, Any] = {}
    with ExitStack() as stack:
        for p in patches:
            started[getattr(p, "attribute", repr(p))] = stack.enter_context(p)
        response = await handle_done(db=db, neo=MagicMock(), session_id=11)
    return started, response


async def test_narrator_never_sees_a_topic_excluded_from_the_grade():
    started, _response = await _run()

    narrated = started["generate_diagnostic"].call_args.kwargs["topic_score"]
    assert [topic.canonical_key for topic in narrated.topics] == ["eq.taught"]
    # The grade itself is carried over, never recomputed from the shorter list.
    assert (narrated.score, narrated.letter) == (100, "A+")


async def test_the_served_topics_payload_still_carries_the_unprobed_row():
    """The UI needs it to say "not part of this grade" — filtering is a
    narrative-input concern only, never a truncation of the record."""
    _started, response = await _run()

    served = {topic["canonical_key"]: topic for topic in response["topics"]}
    assert served["eq.never"]["status"] == "unprobed"
    assert served["eq.never"]["weight"] == 0.0


# --- the artifact / scorecard surface --------------------------------------


def _artifact() -> dict:
    return build_llm_artifact(
        coverage={
            "per_step": {"eq.taught": "covered", "eq.never": "missing"},
            "confidences": {},
        },
        rubric={"overall": {"score": 100}},
        latency_ms=1,
        clarification_trace=[],
        topic_score=_result(),
    )


def test_unprobed_nodes_get_their_own_ledger_status():
    ledger = {row["canonical_key"]: row["status"] for row in _artifact()["node_ledger"]}

    assert ledger == {"eq.taught": "credited", "eq.never": LEDGER_STATUS_UNPROBED}


def test_unprobed_nodes_leave_the_node_coverage_ratio():
    """Both sides of the ratio, or excluding a node from the grade would still
    depress the artifact's coverage number."""
    assert _artifact()["scores"]["node_coverage"] == 1.0


def test_scorecard_never_tells_the_student_to_explain_an_unprobed_node():
    scorecard = render_scorecard(_artifact())

    assert [item["key"] for item in scorecard["taught_well"]] == ["eq.taught"]
    assert scorecard["missing_or_unclear"] == []


def test_no_topic_score_leaves_the_ledger_shape_unchanged():
    """Soft-failed topic scoring (`topic_score=None`) must reproduce the
    pre-fix artifact exactly — no node is ever reclassified without evidence."""
    artifact = build_llm_artifact(
        coverage={"per_step": {"a": "covered", "b": "missing"}, "confidences": {}},
        rubric={"overall": {"score": 50}},
        latency_ms=1,
        clarification_trace=[],
    )

    assert {row["canonical_key"]: row["status"] for row in artifact["node_ledger"]} == {
        "a": "credited",
        "b": "unresolved",
    }
    assert artifact["scores"]["node_coverage"] == 0.5
