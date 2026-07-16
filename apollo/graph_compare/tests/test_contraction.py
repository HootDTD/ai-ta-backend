"""DAG-5 deterministic edge-contraction grading regression tests."""

from __future__ import annotations

import pytest

from apollo.graph_compare.core import grade_attempt
from apollo.graph_compare.findings import FindingKind
from apollo.ontology.edges import EdgeType
from apollo.ontology.nodes import NodeType

from ._builders import cedge, cnode, path, rgraph, rnode, snorm


def _fixture(*, middle_type: NodeType = "simplification", middle_symbolic: str = "2*x = 4"):
    reference = rgraph(
        nodes=(
            rnode("p", symbolic="x = 2"),
            rnode("m", middle_type, symbolic=middle_symbolic),
            rnode("t", symbolic="x = 2"),
        ),
        edges=(
            cedge(EdgeType.DEPENDS_ON, "p", "m"),
            cedge(EdgeType.DEPENDS_ON, "m", "t"),
        ),
        paths=(path("p", "m", "t"),),
    )
    student = snorm(
        nodes=(
            cnode("p", evidence_spans=("start with x + x = 4",)),
            cnode("t", evidence_spans=("combine to get 2*x = 4, so x = 2",)),
        ),
        edges=(cedge(EdgeType.DEPENDS_ON, "p", "t", provenance="explicit"),),
    )
    return student, reference


def _findings(result, kind: FindingKind):
    return tuple(finding for finding in result.findings if finding.kind == kind)


def test_contraction_disabled_preserves_missing_node():
    student, reference = _fixture(middle_type="equation")

    result = grade_attempt(student, reference)

    assert result.coverage_score == pytest.approx(2 / 3)
    assert _findings(result, FindingKind.MISSING_NODE)
    assert not _findings(result, FindingKind.COVERED_BY_CONTRACTION)


def test_symbolically_equivalent_bridge_gets_contraction_credit():
    student, reference = _fixture(middle_type="equation")

    result = grade_attempt(student, reference, contraction_enabled=True)

    assert result.coverage_score == 1.0
    finding = _findings(result, FindingKind.COVERED_BY_CONTRACTION)[0]
    assert finding.message.endswith("decision symbolic_equivalent")


def test_non_symbolic_intermediate_fails_closed_without_model_tier():
    student, reference = _fixture()

    result = grade_attempt(student, reference, contraction_enabled=True)

    assert result.coverage_score == pytest.approx(2 / 3)
    finding = _findings(result, FindingKind.NOT_DEMONSTRATED)[0]
    assert finding.message.endswith("decision unproved")


def test_symbolically_wrong_bridge_fails_closed():
    student, reference = _fixture(middle_type="equation", middle_symbolic="2*x = 5")

    result = grade_attempt(student, reference, contraction_enabled=True)

    assert result.coverage_score == pytest.approx(2 / 3)
    finding = _findings(result, FindingKind.NOT_DEMONSTRATED)[0]
    assert finding.message.endswith(("decision symbolic_veto", "decision unproved"))
