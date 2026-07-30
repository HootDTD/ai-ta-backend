"""2026-07-10 topic-score design spec — wiring ``compute_topic_score`` end to
end in ``handle_done``.

Post flag-reset the topic score is served UNCONDITIONALLY whenever it computes
(the topic-score serving flag was deleted): ``rubric.overall`` becomes the topic
score, ``topics[]`` ships, and XP derives from the replaced overall. The only
surviving branch is the soft-fail: ``compute_topic_score`` raising ->
``topic_score`` is ``None`` -> the legacy rubric is served (no ``topics`` key).

Covers:
  * ``write_artifacts`` always receives a ``topic_score`` kwarg (compute-always);
  * ``topics[]`` present with the expected shape, legacy axes still present,
    ``rubric.overall`` == topic score, XP derived from the REPLACED overall;
  * soft-fail: ``compute_topic_score`` raising -> Done still returns normally,
    no ``topics`` key, legacy rubric served, artifact ``topic_score`` is None.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done import handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.ontology import KGGraph, build_node
from apollo.overseer.rubric import score_to_letter
from apollo.overseer.xp import compute_xp_earned

pytestmark = pytest.mark.unit

_OLD_RUBRIC = {
    "overall": {"score": 90, "letter": "A"},
    "procedure": {"score": 90, "letter": "A", "present": True},
    "justification": {"score": 90, "letter": "A", "present": True},
    "simplification": {"score": 90, "letter": "A", "present": True},
}


def _patches_with_rubric(patches, rubric):
    kept = [p for p in patches if getattr(p, "attribute", None) != "compute_rubric"]
    kept.append(patch("apollo.handlers.done.compute_rubric", return_value=dict(rubric)))
    return kept


def _patches_with_real_xp(patches):
    """Drop the shared harness's fixed ``compute_xp_earned``/
    ``compute_progress_envelope`` mocks (which always yield ``xp_earned=10``
    regardless of input) so the REAL xp module runs end to end — needed to
    prove the XP-ordering claim (XP must be computed from whichever
    ``rubric.overall`` was current at that point in ``handle_done``)."""
    return [
        p
        for p in patches
        if getattr(p, "attribute", None) not in ("compute_xp_earned", "compute_progress_envelope")
    ]


def _reference_graph_with_topics() -> KGGraph:
    """One fully-covered equation node + one missing condition node, so the
    topic score is non-trivial (not the empty-graph 0/F default)."""
    eq = build_node(
        node_type="equation",
        node_id="eq1",
        attempt_id=99,
        source="reference",
        content={"symbolic": "P + 0.5*rho*v**2 = const", "label": "Bernoulli"},
    )
    cond = build_node(
        node_type="condition",
        node_id="c1",
        attempt_id=99,
        source="reference",
        content={"applies_when": "steady flow", "label": ""},
    )
    return KGGraph(nodes=[eq, cond], edges=[])


async def _run(
    monkeypatch,
    *,
    topic_score_side_effect=None,
    write_mock=None,
    use_real_xp=False,
    capture=None,
    diagnostic_result=None,
    remediation_retrieve=None,
):
    db, _sess, _attempt, patches = _old_path_patches()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    if capture is not None:
        capture["attempt"] = _attempt
    graph = _reference_graph_with_topics()

    async def _find_problem_with_graph(_db, _cid, _code, *, course_id):
        assert course_id == _sess.course_id
        problem = MagicMock()
        problem.id = "p_code"
        problem.concept_id = "bernoulli_principle"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    # Drop the base golden stubs this test overrides: _find_problem (real
    # graph), the transcript coverage (a real coverage dict), and the
    # topic-score neutralizer (this test exercises REAL topic scoring).
    drop = {"_find_problem", "compute_transcript_coverage_with_spans", "compute_topic_score"}
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    if use_real_xp:
        patches = _patches_with_real_xp(patches)
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.compute_transcript_coverage_with_spans",
            new=AsyncMock(
                return_value=(
                    {
                        "per_step": {"eq1": "covered", "c1": "missing"},
                        "procedure_scores": {},
                        "confidences": {"eq1": 0.9},
                    },
                    {},
                )
            ),
        ),
    ]

    if topic_score_side_effect is not None:
        patches.append(
            patch(
                "apollo.handlers.done.compute_topic_score",
                side_effect=topic_score_side_effect,
            )
        )

    if write_mock is not None:
        patches.append(patch("apollo.handlers.done.write_artifacts", new=write_mock))
    if diagnostic_result is not None:
        patches = [p for p in patches if getattr(p, "attribute", None) != "generate_diagnostic"]
        patches.append(
            patch("apollo.handlers.done.generate_diagnostic", return_value=diagnostic_result)
        )
    if remediation_retrieve is not None:
        patches.append(
            patch(
                "apollo.overseer.remediation.retrieve_for_question",
                new=remediation_retrieve,
            )
        )

    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out


# --------------------------------------------------------------------------- #
# Compute-always: write_artifacts receives topic_score; serving is unconditional
# --------------------------------------------------------------------------- #
async def test_topic_score_computed_and_threaded_to_artifact(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out = await _run(monkeypatch, write_mock=write_mock)

    write_mock.assert_awaited_once()
    topic_score = write_mock.await_args.kwargs["topic_score"]
    assert topic_score is not None
    assert len(topic_score.topics) == 2
    # Serving is unconditional — topics ship and the overall is the topic score.
    assert "topics" in out
    assert out["rubric"]["overall"]["score"] == 50


# --------------------------------------------------------------------------- #
# Served: topics[] present, legacy axes present, overall == topic score, XP
# derives from the REPLACED overall
# --------------------------------------------------------------------------- #
async def test_topics_served_with_expected_shape(monkeypatch):
    out = await _run(monkeypatch)

    assert "topics" in out
    topics = out["topics"]
    assert len(topics) == 2
    by_key = {t["canonical_key"]: t for t in topics}
    assert by_key["eq1"]["status"] == "covered"
    assert by_key["eq1"]["credit"] == 1.0
    assert by_key["c1"]["status"] == "missing"
    assert by_key["c1"]["credit"] == 0.0
    for t in topics:
        assert set(t.keys()) == {
            "canonical_key",
            "display_name",
            "credit",
            "status",
            "weight",
            "evidence_span",
            "hoot_assisted",
            "misconceptions",
        }
        assert t["evidence_span"] is None
        # No asides in this harness -> the additive flag serves False.
        assert t["hoot_assisted"] is False


async def test_feedback_served_with_topic_score_and_flattened_narrative(monkeypatch):
    feedback = {
        "headline": "You built a useful foundation.",
        "topic_feedback": [
            {"canonical_key": "eq1", "note": "Your equation was clear.", "quote": None},
            {"canonical_key": "c1", "note": "Add the condition.", "quote": None},
        ],
        "recap": [],
        "next_step": "State when the equation applies.",
    }
    flattened = (
        "You built a useful foundation.\n\n"
        "Your equation was clear.\n\n"
        "Add the condition.\n\n"
        "Next step: State when the equation applies."
    )

    out = await _run(
        monkeypatch,
        diagnostic_result=(flattened, feedback),
    )

    assert out["feedback"] == feedback
    assert out["diagnostic_narrative"] == flattened


def _remediation_snippet():
    from config.contracts import BundleSnippet

    return BundleSnippet(
        id="chunk-1",
        type="body",
        page=18,
        section_path="2.1",
        text="Steady flow is required.",
        figure_id=None,
        why="hit",
        source_path="course.pdf",
        doc_title="Course Text",
        doc_short="Course Text",
        citation_marker="[Textbook, p. 18]",
        final_score={"final": 0.95},
        metadata={"document_id": 55},
    )


async def test_remediation_flag_on_adds_review_with_mocked_retrieval(monkeypatch):
    monkeypatch.setenv("INTERACTION3", "true")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    retrieve = AsyncMock(return_value=([_remediation_snippet()], {}))
    feedback = {
        "headline": "You built a useful foundation.",
        "topic_feedback": [
            {"canonical_key": "eq1", "note": "Your equation was clear.", "quote": None},
            {"canonical_key": "c1", "note": "Add the condition.", "quote": None},
        ],
        "recap": [],
        "next_step": "State when the equation applies.",
    }

    out = await _run(
        monkeypatch,
        diagnostic_result=("flattened", feedback),
        remediation_retrieve=retrieve,
    )

    assert "review" not in out["feedback"]["topic_feedback"][0]
    assert out["feedback"]["topic_feedback"][1]["review"] == [
        {"doc_id": 55, "label": "[Textbook, p. 18]", "page": 18, "upload_id": None}
    ]
    retrieve.assert_awaited_once()


async def test_remediation_matching_concept_adds_review(monkeypatch):
    monkeypatch.setenv("INTERACTION3", "true")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "ethics, BERNOULLI_PRINCIPLE")
    retrieve = AsyncMock(return_value=([_remediation_snippet()], {}))
    feedback = {
        "headline": "Headline",
        "topic_feedback": [
            {"canonical_key": "eq1", "note": "Strong.", "quote": None},
            {"canonical_key": "c1", "note": "Review this.", "quote": None},
        ],
        "recap": [],
        "next_step": "Try again.",
    }

    out = await _run(
        monkeypatch,
        diagnostic_result=("flattened", feedback),
        remediation_retrieve=retrieve,
    )

    assert out["feedback"]["topic_feedback"][1]["review"] == [
        {"doc_id": 55, "label": "[Textbook, p. 18]", "page": 18, "upload_id": None}
    ]
    retrieve.assert_awaited_once()


async def test_remediation_non_matching_concept_leaves_feedback_untouched(monkeypatch):
    monkeypatch.setenv("INTERACTION3", "true")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "ethics")
    retrieve = AsyncMock(return_value=([_remediation_snippet()], {}))
    feedback = {
        "headline": "Headline",
        "topic_feedback": [
            {"canonical_key": "eq1", "note": "Strong.", "quote": None},
            {"canonical_key": "c1", "note": "Review this.", "quote": None},
        ],
        "recap": [],
        "next_step": "Try again.",
    }

    out = await _run(
        monkeypatch,
        diagnostic_result=("flattened", feedback),
        remediation_retrieve=retrieve,
    )

    assert out["feedback"] == feedback
    assert all("review" not in item for item in out["feedback"]["topic_feedback"])
    retrieve.assert_not_awaited()


async def test_remediation_flag_off_leaves_feedback_untouched(monkeypatch):
    monkeypatch.delenv("INTERACTION3", raising=False)
    retrieve = AsyncMock(return_value=([_remediation_snippet()], {}))
    feedback = {
        "headline": "Headline",
        "topic_feedback": [
            {"canonical_key": "eq1", "note": "Strong.", "quote": None},
            {"canonical_key": "c1", "note": "Review this.", "quote": None},
        ],
        "recap": [],
        "next_step": "Try again.",
    }

    out = await _run(
        monkeypatch,
        diagnostic_result=("flattened", feedback),
        remediation_retrieve=retrieve,
    )

    assert out["feedback"] == feedback
    retrieve.assert_not_awaited()


async def test_remediation_failure_leaves_grade_payload_untouched(monkeypatch):
    retrieve = AsyncMock(side_effect=RuntimeError("retrieval unavailable"))
    feedback = {
        "headline": "Headline",
        "topic_feedback": [
            {"canonical_key": "eq1", "note": "Strong.", "quote": None},
            {"canonical_key": "c1", "note": "Review this.", "quote": None},
        ],
        "recap": [],
        "next_step": "Try again.",
    }

    monkeypatch.delenv("INTERACTION3", raising=False)
    expected = await _run(
        monkeypatch,
        diagnostic_result=("flattened", deepcopy(feedback)),
        remediation_retrieve=retrieve,
    )

    monkeypatch.setenv("INTERACTION3", "1")
    out = await _run(
        monkeypatch,
        diagnostic_result=("flattened", deepcopy(feedback)),
        remediation_retrieve=retrieve,
    )

    assert out == expected
    assert out["feedback"] == feedback
    assert out["rubric"]["overall"] == {"score": 50, "letter": score_to_letter(50)}
    assert "review" not in out["feedback"]["topic_feedback"][1]
    retrieve.assert_awaited_once()


async def test_feedback_absent_without_topic_score_even_if_diagnostic_returns_one(monkeypatch):
    feedback = {
        "headline": "Headline",
        "topic_feedback": [],
        "recap": [],
        "next_step": "Try again.",
    }
    out = await _run(
        monkeypatch,
        topic_score_side_effect=RuntimeError("boom"),
        diagnostic_result=("legacy", feedback),
    )

    assert out["diagnostic_narrative"] == "legacy"
    assert "feedback" not in out


async def test_feedback_absent_on_diagnostic_json_soft_fail(monkeypatch):
    out = await _run(
        monkeypatch,
        diagnostic_result=("legacy ledger narrative", None),
    )

    assert "topics" in out
    assert out["diagnostic_narrative"] == "legacy ledger narrative"
    assert "feedback" not in out


async def test_legacy_axes_still_present(monkeypatch):
    out = await _run(monkeypatch)

    rubric = out["rubric"]
    assert rubric["procedure"] == _OLD_RUBRIC["procedure"]
    assert rubric["justification"] == _OLD_RUBRIC["justification"]
    assert rubric["simplification"] == _OLD_RUBRIC["simplification"]


async def test_overall_equals_topic_score(monkeypatch):
    out = await _run(monkeypatch)

    # eq1 covered (credit 1.0), c1 missing (credit 0.0) — with equal
    # centrality (no DEPENDS_ON/PRECEDES edges -> both floor identically),
    # coverage_component = 0.5, no misconceptions -> score 50.
    assert out["rubric"]["overall"]["score"] == 50
    assert out["rubric"]["overall"]["letter"] == score_to_letter(50)


async def test_xp_derives_from_replaced_overall(monkeypatch):
    """THE ordering proof: served_rubric (topic score) must be built BEFORE
    xp_earned is computed, so XP follows the topic score, not the axis
    blend (90)."""
    out = await _run(monkeypatch, use_real_xp=True)

    expected_xp = compute_xp_earned(
        overall_score=out["rubric"]["overall"]["score"],
        difficulty="intro",
        is_reattempt=False,
    )
    assert out["xp_earned"] == expected_xp
    # Sanity: this is NOT the axis-blend XP (90) — proves XP actually moved.
    axis_xp = compute_xp_earned(overall_score=90, difficulty="intro", is_reattempt=False)
    assert out["xp_earned"] != axis_xp


async def test_artifact_receives_same_topic_score_as_served(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out = await _run(monkeypatch, write_mock=write_mock)

    write_mock.assert_awaited_once()
    artifact_topic_score = write_mock.await_args.kwargs["topic_score"]
    assert artifact_topic_score is not None
    assert artifact_topic_score.score == out["rubric"]["overall"]["score"]


# --------------------------------------------------------------------------- #
# Soft-fail: compute_topic_score raising must never break Done (legacy rubric)
# --------------------------------------------------------------------------- #
async def test_topic_score_raising_soft_fails_legacy_rubric_served(monkeypatch):
    out = await _run(monkeypatch, topic_score_side_effect=RuntimeError("boom"))

    assert "topics" not in out
    # topic_score computation failed -> falls back to the OLD rubric
    # (served_rubric is rubric itself).
    assert out["rubric"] == _OLD_RUBRIC
    assert "rubric" in out  # HTTP 200 shape, no exception escaped


# --------------------------------------------------------------------------- #
# served_overall snapshot: the persisted diagnostic_report records the grade
# the student was SHOWN, while `rubric` stays the raw axis rubric
# --------------------------------------------------------------------------- #
async def test_served_overall_snapshot_persisted(monkeypatch):
    """Browse/progress re-serve `served_overall`; rerun consumers keep the raw
    rubric — both must be true on the same persisted report."""
    capture: dict = {}
    out = await _run(monkeypatch, capture=capture)

    report = capture["attempt"].diagnostic_report
    assert report["served_overall"] == out["rubric"]["overall"]
    # Topic score (50 in this harness) was served — the snapshot records it...
    assert report["served_overall"]["score"] == 50
    # ...while the persisted raw rubric keeps the axis blend untouched.
    assert report["rubric"]["overall"]["score"] == 90


async def test_served_overall_snapshot_on_soft_fail_equals_raw_overall(monkeypatch):
    """When topic scoring soft-fails, the student saw the legacy overall, so
    the snapshot must equal `rubric.overall` (still present, still a copy)."""
    capture: dict = {}
    out = await _run(
        monkeypatch, topic_score_side_effect=RuntimeError("boom"), capture=capture
    )

    report = capture["attempt"].diagnostic_report
    assert report["served_overall"] == {"score": 90, "letter": "A"}
    assert report["served_overall"] == out["rubric"]["overall"]


async def test_topic_score_raising_artifact_receives_none(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out = await _run(
        monkeypatch,
        topic_score_side_effect=RuntimeError("boom"),
        write_mock=write_mock,
    )

    write_mock.assert_awaited_once()
    assert write_mock.await_args.kwargs["topic_score"] is None
    assert "topics" not in out
