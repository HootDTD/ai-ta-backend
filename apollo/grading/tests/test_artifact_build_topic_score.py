"""2026-07-10 topic-score design spec §2/§3 — wire ``TopicScoreResult`` into
both ``build_llm_artifact`` and ``build_graph_artifact`` as a NEW opt-in
``topic_score`` keyword, serialized into ``scores["topic_score"]`` via the
shared ``topic_score_serialize.serialize_topic_score``.

``None`` (the default) leaves both builders byte-identical to today — no
``topic_score`` key at all in ``scores`` (never ``null``). A provided result
nests the full serialized block. Pure module: no IO.
"""

from __future__ import annotations

import pytest

from apollo.grading.artifact_build import build_graph_artifact, build_llm_artifact
from apollo.grading.audited_grade import AuditedGrade
from apollo.grading.composite import load_weights
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult
from apollo.resolution.result import ResolutionResult

_COVERAGE = {
    "per_step": {"k1": "covered", "k2": "missing"},
    "confidences": {"k1": 0.9, "k2": 0.0},
}
_RUBRIC = {"overall": {"score": 71}}


def _llm_kwargs(**overrides) -> dict:
    kwargs = dict(
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        weights=load_weights(),
        graph_failure=None,
        latency_ms=5,
        clarification_trace=[],
    )
    kwargs.update(overrides)
    return kwargs


def _topic_score_result() -> TopicScoreResult:
    return TopicScoreResult(
        score=80,
        letter="B+",
        coverage_component=0.9,
        misconception_dock=0.1,
        topics=(
            TopicCredit(
                canonical_key="eq1",
                display_name="Bernoulli equation",
                credit=1.0,
                status="covered",
                weight=0.6,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.sign_flip",
                        resolved=False,
                        dock_points=0.1,
                        evidence_span="pressure always increases",
                    ),
                ),
            ),
            TopicCredit(
                canonical_key="c1",
                display_name=None,
                credit=0.0,
                status="missing",
                weight=0.4,
                misconceptions=(),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# build_llm_artifact
# --------------------------------------------------------------------------- #
def test_llm_artifact_topic_score_default_none_byte_identical_to_no_kwarg():
    without_kwarg = build_llm_artifact(**_llm_kwargs())
    with_none = build_llm_artifact(**_llm_kwargs(), topic_score=None)

    assert without_kwarg == with_none
    assert "topic_score" not in without_kwarg["scores"]


def test_llm_artifact_topic_score_nests_serialized_block():
    result = _topic_score_result()
    art = build_llm_artifact(**_llm_kwargs(), topic_score=result)

    block = art["scores"]["topic_score"]
    assert block["score"] == 80
    assert block["letter"] == "B+"
    assert block["coverage_component"] == pytest.approx(0.9)
    assert block["misconception_dock"] == pytest.approx(0.1)
    assert len(block["topics"]) == 2

    covered = block["topics"][0]
    assert covered["canonical_key"] == "eq1"
    assert covered["display_name"] == "Bernoulli equation"
    assert covered["credit"] == 1.0
    assert covered["status"] == "covered"
    assert covered["weight"] == pytest.approx(0.6)
    assert covered["misconceptions"] == [
        {
            "canonical_key": "misc.sign_flip",
            "resolved": False,
            "dock_points": pytest.approx(0.1),
            "evidence_span": "pressure always increases",
        }
    ]

    missing = block["topics"][1]
    assert missing["canonical_key"] == "c1"
    assert missing["display_name"] is None
    assert missing["misconceptions"] == []


def test_llm_artifact_topic_score_does_not_change_other_keys():
    """Only ``scores.topic_score`` differs — every other key (rubric-derived
    composite, node_coverage, node_ledger, etc.) is untouched."""
    without = build_llm_artifact(**_llm_kwargs())
    with_topic = build_llm_artifact(**_llm_kwargs(), topic_score=_topic_score_result())

    for key in without:
        if key == "scores":
            continue
        assert with_topic[key] == without[key]

    for key in without["scores"]:
        assert with_topic["scores"][key] == without["scores"][key]


# --------------------------------------------------------------------------- #
# build_graph_artifact
# --------------------------------------------------------------------------- #
def _grade(findings: tuple[Finding, ...]) -> GradeResult:
    return GradeResult(
        coverage_score=0.5,
        soundness_score=0.5,
        bisimilarity_score=0.5,
        node_coverage_score=0.5,
        edge_coverage_score=0.5,
        scoping_score=1.0,
        usage_score=1.0,
        procedure_order_score=1.0,
        dependency_score=1.0,
        contradiction_score=1.0,
        comparison_confidence=1.0,
        findings=findings,
        comparison_version=COMPARISON_VERSION,
    )


@pytest.fixture
def shadow_fixture() -> ShadowGradeResult:
    findings: tuple[Finding, ...] = (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="eq.a",
            student_node_ids=("n_a",),
            evidence_spans=("eq a is conserved",),
            confidence=0.92,
        ),
    )
    grade = _grade(findings)
    audited = AuditedGrade(
        grade=grade,
        findings=findings,
        abstention_reasons=(),
        abstained=False,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    return ShadowGradeResult(
        run_id=1,
        grade=grade,
        audited=audited,
        normalization_confidence=0.8,
        reference_graph_hash="refhash-v1:deadbeef",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=ResolutionResult(resolved=(), tier_counts={}, llm_calls=0),
    )


def test_graph_artifact_topic_score_default_none_byte_identical_to_no_kwarg(shadow_fixture):
    without_kwarg = build_graph_artifact(
        shadow=shadow_fixture,
        weights=load_weights(),
        clarification_trace=[],
        latency_ms=None,
    )
    with_none = build_graph_artifact(
        shadow=shadow_fixture,
        weights=load_weights(),
        clarification_trace=[],
        latency_ms=None,
        topic_score=None,
    )

    assert without_kwarg == with_none
    assert "topic_score" not in without_kwarg["scores"]


def test_graph_artifact_topic_score_nests_serialized_block(shadow_fixture):
    result = _topic_score_result()
    art = build_graph_artifact(
        shadow=shadow_fixture,
        weights=load_weights(),
        clarification_trace=[],
        latency_ms=None,
        topic_score=result,
    )

    block = art["scores"]["topic_score"]
    assert block["score"] == 80
    assert block["letter"] == "B+"
    assert len(block["topics"]) == 2
    assert block["topics"][0]["canonical_key"] == "eq1"
