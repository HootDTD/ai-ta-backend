"""2026-07-10 topic-score design spec — wiring ``compute_topic_score`` end to
end in ``handle_done``.

Mirrors the established flag-golden / done-route harness patterns:
``test_misconception_flag_off_golden.py`` (byte-identical flag-off goldens)
and ``test_done_misconception.py`` (the ``_old_path_patches``-based route
harness, real reference-graph nodes threaded through the REAL detector/gate/
merge chain so the wiring is exercised for real, not mocked away).

Covers:
  * Compute-always (flag-independent): ``write_artifacts`` always receives a
    ``topic_score`` kwarg once ``APOLLO_GRADING_ARTIFACT_ENABLED`` is on,
    regardless of ``APOLLO_TOPIC_SCORE_SERVED``.
  * Flag-off golden: served payload byte-identical (no ``topics`` key, no
    ``rubric`` change) with ``APOLLO_TOPIC_SCORE_SERVED`` unset.
  * Flag-on route test: ``topics[]`` present, legacy axes still present,
    ``rubric.overall`` == topic score, XP derived from the REPLACED overall.
  * Soft-fail: ``compute_topic_score`` raising -> Done still returns normally,
    no ``topics`` key, no artifact ``topic_score`` kwarg reaching a non-None
    value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done import handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches
from apollo.ontology import KGGraph, build_node
from apollo.overseer.misconception_detector.types import ConceptFinding, DetectionResult
from apollo.overseer.rubric import score_to_letter
from apollo.overseer.xp import compute_xp_earned

pytestmark = pytest.mark.unit

_SERVED_FLAG = "APOLLO_TOPIC_SCORE_SERVED"
_DETECTOR_FLAG = "APOLLO_MISCONCEPTION_DETECTOR"

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


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv(_SERVED_FLAG, raising=False)
    monkeypatch.delenv(_DETECTOR_FLAG, raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRADING_ARTIFACT_ENABLED", raising=False)
    yield


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
    served_flag,
    detector_flag=None,
    artifact_flag=None,
    detect_return=None,
    topic_score_side_effect=None,
    write_mock=None,
    use_real_xp=False,
):
    if served_flag is not None:
        monkeypatch.setenv(_SERVED_FLAG, served_flag)
    if detector_flag is not None:
        monkeypatch.setenv(_DETECTOR_FLAG, detector_flag)
    if artifact_flag is not None:
        monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", artifact_flag)

    db, _sess, _attempt, patches = _old_path_patches()
    graph = _reference_graph_with_topics()

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    patches = [p for p in patches if getattr(p, "attribute", None) != "_find_problem"]
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    if use_real_xp:
        patches = _patches_with_real_xp(patches)
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.compute_coverage",
            new=AsyncMock(
                return_value={
                    "per_step": {"eq1": "covered", "c1": "missing"},
                    "procedure_scores": {},
                    "confidences": {"eq1": 0.9},
                }
            ),
        ),
    ]

    if detector_flag == "true":
        detection = detect_return if detect_return is not None else DetectionResult(per_concept=())
        patches += [
            patch(
                "apollo.handlers.done.detect_misconceptions",
                new=AsyncMock(return_value=detection),
            ),
            patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
            patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
            patch(
                "apollo.handlers.done._student_utterances",
                new=AsyncMock(return_value=("pressure always increases",)),
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

    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out


# --------------------------------------------------------------------------- #
# Flag OFF: byte-identical golden (topics absent, rubric untouched)
# --------------------------------------------------------------------------- #
async def test_flag_off_golden_no_topics_key_rubric_unchanged(monkeypatch):
    out = await _run(monkeypatch, served_flag=None)

    assert "topics" not in out
    assert out["rubric"] == _OLD_RUBRIC
    assert out["rubric"]["overall"]["score"] == 90


async def test_flag_explicit_false_is_also_byte_identical(monkeypatch):
    out = await _run(monkeypatch, served_flag="false")

    assert "topics" not in out
    assert out["rubric"] == _OLD_RUBRIC


async def test_flag_off_xp_derives_from_old_rubric(monkeypatch):
    out = await _run(monkeypatch, served_flag=None, use_real_xp=True)

    expected_xp = compute_xp_earned(overall_score=90, difficulty="intro", is_reattempt=False)
    assert out["xp_earned"] == expected_xp


# --------------------------------------------------------------------------- #
# Compute-always: write_artifacts receives topic_score regardless of serving
# --------------------------------------------------------------------------- #
async def test_topic_score_computed_and_threaded_to_artifact_even_when_flag_off(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out = await _run(
        monkeypatch,
        served_flag=None,
        artifact_flag="true",
        write_mock=write_mock,
    )

    write_mock.assert_awaited_once()
    topic_score = write_mock.await_args.kwargs["topic_score"]
    assert topic_score is not None
    assert len(topic_score.topics) == 2
    # Serving stayed off — the student payload is unaffected.
    assert "topics" not in out
    assert out["rubric"] == _OLD_RUBRIC


# --------------------------------------------------------------------------- #
# Flag ON: topics[] served, legacy axes present, overall == topic score, XP
# derives from the REPLACED overall
# --------------------------------------------------------------------------- #
async def test_flag_on_topics_served_with_expected_shape(monkeypatch):
    out = await _run(monkeypatch, served_flag="true")

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
            "misconceptions",
        }


async def test_flag_on_legacy_axes_still_present(monkeypatch):
    out = await _run(monkeypatch, served_flag="true")

    rubric = out["rubric"]
    assert rubric["procedure"] == _OLD_RUBRIC["procedure"]
    assert rubric["justification"] == _OLD_RUBRIC["justification"]
    assert rubric["simplification"] == _OLD_RUBRIC["simplification"]


async def test_flag_on_overall_equals_topic_score(monkeypatch):
    out = await _run(monkeypatch, served_flag="true")

    # eq1 covered (credit 1.0), c1 missing (credit 0.0) — with equal
    # centrality (no DEPENDS_ON/PRECEDES edges -> both floor identically),
    # coverage_component = 0.5, no misconceptions -> score 50.
    assert out["rubric"]["overall"]["score"] == 50
    assert out["rubric"]["overall"]["letter"] == score_to_letter(50)


async def test_flag_on_xp_derives_from_replaced_overall(monkeypatch):
    """THE ordering proof: served_rubric (topic score) must be built BEFORE
    xp_earned is computed, so XP follows the topic score, not the axis
    blend (90)."""
    out = await _run(monkeypatch, served_flag="true", use_real_xp=True)

    expected_xp = compute_xp_earned(
        overall_score=out["rubric"]["overall"]["score"],
        difficulty="intro",
        is_reattempt=False,
    )
    assert out["xp_earned"] == expected_xp
    # Sanity: this is NOT the axis-blend XP (90) — proves XP actually moved.
    axis_xp = compute_xp_earned(overall_score=90, difficulty="intro", is_reattempt=False)
    assert out["xp_earned"] != axis_xp


async def test_flag_on_artifact_receives_same_topic_score_as_served(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out = await _run(
        monkeypatch,
        served_flag="true",
        artifact_flag="true",
        write_mock=write_mock,
    )

    write_mock.assert_awaited_once()
    artifact_topic_score = write_mock.await_args.kwargs["topic_score"]
    assert artifact_topic_score is not None
    assert artifact_topic_score.score == out["rubric"]["overall"]["score"]


async def test_flag_on_with_docked_misconception_appears_in_topics(monkeypatch):
    """The detector ON + a real docked finding localizes onto eq1's
    misconceptions[] in the served topics list."""
    finding = ConceptFinding(
        concept_key="eq1",
        verdict="misconception",
        confidence=1.0,
        severity=0.0,
        evidence_span="pressure always increases",
        signature="misc.sign_flip",
        source="sympy_veto",
        corroborated=True,
    )
    detection = DetectionResult(per_concept=(finding,))

    out = await _run(
        monkeypatch,
        served_flag="true",
        detector_flag="true",
        detect_return=detection,
    )

    by_key = {t["canonical_key"]: t for t in out["topics"]}
    assert len(by_key["eq1"]["misconceptions"]) == 1
    misc = by_key["eq1"]["misconceptions"][0]
    assert misc["canonical_key"] == "misc.sign_flip"
    assert misc["evidence_span"] == "pressure always increases"


# --------------------------------------------------------------------------- #
# Soft-fail: compute_topic_score raising must never break Done
# --------------------------------------------------------------------------- #
async def test_topic_score_raising_soft_fails_no_topics_key_http_200_shape(monkeypatch):
    out = await _run(
        monkeypatch,
        served_flag="true",
        topic_score_side_effect=RuntimeError("boom"),
    )

    assert "topics" not in out
    # Serving flag was ON but topic_score computation failed -> falls back to
    # the OLD rubric (served_rubric is rubric itself).
    assert out["rubric"] == _OLD_RUBRIC
    assert "rubric" in out  # HTTP 200 shape, no exception escaped


async def test_topic_score_raising_artifact_receives_none(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out = await _run(
        monkeypatch,
        served_flag="true",
        artifact_flag="true",
        topic_score_side_effect=RuntimeError("boom"),
        write_mock=write_mock,
    )

    write_mock.assert_awaited_once()
    assert write_mock.await_args.kwargs["topic_score"] is None
    assert "topics" not in out


async def test_topic_score_raising_flag_off_also_soft_fails(monkeypatch):
    """Compute-always means the soft-fail matters even when serving is off."""
    out = await _run(
        monkeypatch,
        served_flag=None,
        topic_score_side_effect=RuntimeError("boom"),
    )

    assert "topics" not in out
    assert out["rubric"] == _OLD_RUBRIC
