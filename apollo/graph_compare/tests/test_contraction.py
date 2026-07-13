"""DAG-5 edge-contraction grading regression tests (pure, no model load)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import apollo.graph_compare.contraction as contraction
import apollo.handlers.done as done
import apollo.handlers.done_grading as done_grading
from apollo.grading.events import convert_findings_to_events
from apollo.grading.rubric_mapping import findings_to_rubric_input
from apollo.grading.tests._builders import audited
from apollo.graph_compare.contraction import contraction_verdicts
from apollo.graph_compare.core import grade_attempt
from apollo.graph_compare.findings import FindingKind
from apollo.ontology.edges import EdgeType
from apollo.ontology.nodes import NodeType
from apollo.resolution.nli_adjudicator import NLIResult

from ._builders import cedge, cnode, path, rgraph, rnode, snorm


class _Adjudicator:
    def __init__(self, result: NLIResult):
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.calls.append((premise, hypothesis))
        return self.result


class _FailingAdjudicator:
    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        raise RuntimeError("model unavailable")


class _SequenceAdjudicator:
    def __init__(self, results: tuple[NLIResult, ...]):
        self.results = iter(results)
        self.hypotheses: list[str] = []

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.hypotheses.append(hypothesis)
        return next(self.results)


ENTAILS = NLIResult("entailment", 0.96, 0.01, 0.03, "fake")
NEUTRAL = NLIResult("neutral", 0.05, 0.01, 0.94, "fake")
CONTRADICTED = NLIResult("entailment", 0.96, 0.20, 0.0, "fake")


def _fixture(
    *,
    middle_type: NodeType = "simplification",
    include_predecessor: bool = True,
    include_successor: bool = True,
    bridge: bool = True,
):
    ref = rgraph(
        nodes=(
            rnode("p", symbolic="x = 2"),
            rnode("m", middle_type, symbolic="2x = 4"),
            rnode("t", symbolic="x = 2"),
        ),
        edges=(
            cedge(EdgeType.DEPENDS_ON, "p", "m"),
            cedge(EdgeType.DEPENDS_ON, "m", "t"),
        ),
        paths=(path("p", "m", "t"),),
    )
    nodes = []
    if include_predecessor:
        nodes.append(cnode("p", evidence_spans=("start with x + x = 4",)))
    if include_successor:
        nodes.append(cnode("t", evidence_spans=("combine to get 2x = 4, so x = 2",)))
    edges = (
        (cedge(EdgeType.DEPENDS_ON, "p", "t", provenance="explicit"),)
        if bridge and include_predecessor and include_successor
        else ()
    )
    return snorm(nodes=tuple(nodes), edges=edges), ref


@pytest.fixture
def concrete_equation_case():
    """Build the conductor's v2 bridge shape with injectable student evidence."""

    def build(bridge_text: str):
        reference = rgraph(
            nodes=(
                rnode("p", symbolic="v1 = input"),
                rnode("m", symbolic="v2 = A1*v1/A2"),
                rnode("t", symbolic="result = v2"),
            ),
            edges=(
                cedge(EdgeType.DEPENDS_ON, "p", "m"),
                cedge(EdgeType.DEPENDS_ON, "m", "t"),
            ),
            paths=(path("p", "m", "t"),),
        )
        student = snorm(
            nodes=(
                cnode("p", evidence_spans=("Start from the known v1.",)),
                cnode("t", evidence_spans=(bridge_text,)),
            ),
            edges=(cedge(EdgeType.DEPENDS_ON, "p", "t", provenance="explicit"),),
        )
        return student, reference

    return build


def _findings(result, kind: FindingKind):
    return tuple(finding for finding in result.findings if finding.kind == kind)


def test_flag_off_default_is_byte_identical_on_contraction_shape(monkeypatch):
    student, reference = _fixture()
    monkeypatch.setenv("APOLLO_GRAPH_CONTRACTION_ENABLED", "1")

    baseline = grade_attempt(student, reference)
    repeated = grade_attempt(student, reference)

    assert repeated == baseline
    assert baseline.coverage_score == pytest.approx(2 / 3)
    assert _findings(baseline, FindingKind.MISSING_NODE)
    assert not _findings(baseline, FindingKind.COVERED_BY_CONTRACTION)
    assert not _findings(baseline, FindingKind.NOT_DEMONSTRATED)


def test_entailing_bridge_gets_coverage_finding_event_and_edge_diagnostics():
    student, reference = _fixture()
    adjudicator = _Adjudicator(ENTAILS)

    result = grade_attempt(student, reference, contraction_adjudicator=adjudicator)

    assert result.coverage_score == 1.0
    assert result.node_coverage_score == 1.0
    assert not _findings(result, FindingKind.MISSING_NODE)
    (finding,) = _findings(result, FindingKind.COVERED_BY_CONTRACTION)
    assert finding.canonical_key == "m"
    assert finding.student_node_ids == ("n_p", "n_t")
    assert finding.score == 0.96
    assert finding.message == "chain p -> m -> t; bridge p->t (explicit); decision nli"
    assert len(_findings(result, FindingKind.MATCHED_EDGE)) == 2
    assert not _findings(result, FindingKind.MISSING_EDGE)
    assert adjudicator.calls[0][1] == "2x = 4"

    events = convert_findings_to_events(
        audited(result.findings),
        opposes_map={},
        turn_order={},
    )
    event = next(event for event in events if event.canonical_key == "m")
    assert event.event_kind.value == "covered"
    assert event.score == 0.96


def test_traversal_without_entailment_is_neutral_and_not_covered():
    student, reference = _fixture()

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(NEUTRAL),
    )

    assert result.coverage_score == pytest.approx(2 / 3)
    assert not _findings(result, FindingKind.MISSING_NODE)
    (finding,) = _findings(result, FindingKind.NOT_DEMONSTRATED)
    assert finding.canonical_key == "m"
    assert not _findings(result, FindingKind.MATCHED_EDGE)
    assert len(_findings(result, FindingKind.MISSING_EDGE)) == 2
    events = convert_findings_to_events(
        audited(result.findings),
        opposes_map={},
        turn_order={},
    )
    assert all(event.canonical_key != "m" for event in events)
    assert not any(event.event_kind.value == "missing" and event.score == 0.0 for event in events)
    rubric_input = findings_to_rubric_input(
        audited=audited(result.findings),
        reference_graph=reference,
        opposes_map={},
        turn_order={},
    )
    assert rubric_input.coverage["per_step"]["m"] == "not_demonstrated"


@pytest.mark.parametrize(
    ("fixture_kwargs", "mutate_reference"),
    [
        ({"middle_type": "definition"}, None),
        ({"include_predecessor": False}, None),
        ({"include_successor": False}, None),
        ({"bridge": False}, None),
        ({}, "branch"),
    ],
)
def test_ineligible_shapes_stay_plain_missing(fixture_kwargs, mutate_reference):
    student, reference = _fixture(**fixture_kwargs)
    if mutate_reference == "branch":
        reference = rgraph(
            nodes=reference.nodes + (rnode("q"),),
            edges=reference.edges,
            paths=(path("p", "m", "t"), path("q", "m", "t")),
        )

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(ENTAILS),
    )

    assert _findings(result, FindingKind.MISSING_NODE)
    assert not _findings(result, FindingKind.COVERED_BY_CONTRACTION)
    assert not _findings(result, FindingKind.NOT_DEMONSTRATED)
    events = convert_findings_to_events(
        audited(result.findings),
        opposes_map={},
        turn_order={},
    )
    assert any(event.event_kind.value == "missing" and event.score == 0.0 for event in events)


def test_chain_longer_than_three_is_not_contracted():
    keys = ("p", "m1", "m2", "m3", "m4", "t")
    reference = rgraph(
        nodes=tuple(rnode(key, "simplification", symbolic=key) for key in keys),
        paths=(path(*keys),),
    )
    student = snorm(
        nodes=(cnode("p"), cnode("t")),
        edges=(cedge(EdgeType.DEPENDS_ON, "p", "t"),),
    )

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(ENTAILS),
    )

    assert len(_findings(result, FindingKind.MISSING_NODE)) == 4
    assert not _findings(result, FindingKind.COVERED_BY_CONTRACTION)


def test_three_node_chain_is_checked_individually_against_shared_bridge():
    keys = ("p", "m1", "m2", "m3", "t")
    reference = rgraph(
        nodes=tuple(rnode(key, "simplification", symbolic=key) for key in keys),
        paths=(path(*keys),),
    )
    student = snorm(
        nodes=(cnode("p"), cnode("t")),
        edges=(cedge(EdgeType.DEPENDS_ON, "p", "t"),),
    )
    adjudicator = _SequenceAdjudicator((ENTAILS, NEUTRAL, ENTAILS))

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=adjudicator,
    )

    assert result.coverage_score == pytest.approx(4 / 5)
    assert {
        finding.canonical_key for finding in _findings(result, FindingKind.COVERED_BY_CONTRACTION)
    } == {"m1", "m3"}
    assert {
        finding.canonical_key for finding in _findings(result, FindingKind.NOT_DEMONSTRATED)
    } == {"m2"}
    assert adjudicator.hypotheses == ["m1", "m2", "m3"]


def test_contradiction_above_threshold_blocks_contraction_credit():
    student, reference = _fixture()

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(CONTRADICTED),
    )

    assert _findings(result, FindingKind.NOT_DEMONSTRATED)
    assert not _findings(result, FindingKind.COVERED_BY_CONTRACTION)


def test_no_adjudicator_fails_closed_for_eligible_bridge():
    student, reference = _fixture()

    verdicts = contraction_verdicts(
        student,
        reference,
        reference.paths[0],
        None,
    )

    assert verdicts["m"].kind == FindingKind.NOT_DEMONSTRATED
    assert verdicts["m"].entailment is None


def test_adjudicator_runtime_failure_fails_closed():
    student, reference = _fixture()

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_FailingAdjudicator(),
    )

    (finding,) = _findings(result, FindingKind.NOT_DEMONSTRATED)
    assert finding.score is None


def test_covered_interior_key_is_skipped_before_later_contraction():
    keys = ("p", "x", "m", "t")
    reference = rgraph(
        nodes=tuple(rnode(key, "simplification", symbolic=key) for key in keys),
        paths=(path(*keys),),
    )
    student = snorm(
        nodes=(cnode("p"), cnode("x"), cnode("t")),
        edges=(cedge(EdgeType.DEPENDS_ON, "x", "t"),),
    )

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(ENTAILS),
    )

    assert result.coverage_score == 1.0
    assert {finding.canonical_key for finding in result.findings} >= {"p", "x", "m", "t"}


def test_merged_student_node_is_a_bridge_without_an_edge():
    student, reference = _fixture(bridge=False)
    shared_id = ("combined",)
    student = snorm(
        nodes=(
            cnode("p", source_node_ids=shared_id, evidence_spans=("combined proof",)),
            cnode("t", source_node_ids=shared_id, evidence_spans=("combined proof",)),
        )
    )

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(ENTAILS),
    )

    (finding,) = _findings(result, FindingKind.COVERED_BY_CONTRACTION)
    assert finding.student_node_ids == shared_id
    assert finding.evidence_spans == ("combined proof",)
    assert finding.message == "chain p -> m -> t; bridge merged-student-node; decision nli"


def test_successor_branch_is_ineligible():
    student, reference = _fixture()
    reference = rgraph(
        nodes=reference.nodes + (rnode("q"),),
        edges=reference.edges,
        paths=(path("p", "m", "t"), path("p", "m", "q")),
    )

    result = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(ENTAILS),
    )

    assert _findings(result, FindingKind.MISSING_NODE)
    assert not _findings(result, FindingKind.COVERED_BY_CONTRACTION)


def test_runtime_flag_readers_default_off_and_parse_per_call(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_CONTRACTION_ENABLED", raising=False)
    assert done._graph_contraction_enabled() is False
    assert done_grading._graph_contraction_enabled() is False

    monkeypatch.setenv("APOLLO_GRAPH_CONTRACTION_ENABLED", "Yes")
    assert done._graph_contraction_enabled() is True
    assert done_grading._graph_contraction_enabled() is True


def test_contraction_adjudicator_reuses_context_or_fails_closed():
    configured = done_grading._contraction_adjudicator(
        SimpleNamespace(nli=_Adjudicator(ENTAILS), params=done_grading.NLIParams())
    )
    assert configured.classify("bridge", "middle") == ENTAILS

    unavailable = done_grading._contraction_adjudicator(None)
    result = unavailable.classify("bridge", "middle")
    assert result.label == "neutral"
    assert result.entailment == 0.0


def test_prose_graph_is_completely_unaffected():
    student, reference = _fixture(middle_type="definition")

    baseline = grade_attempt(student, reference)
    enabled = grade_attempt(
        student,
        reference,
        contraction_adjudicator=_Adjudicator(ENTAILS),
    )

    assert enabled == baseline


def test_equivalent_fragment_confirms_without_nli(concrete_equation_case):
    student, reference = concrete_equation_case(
        "I multiplied v1 by A1 and divided by A2 to get v2 = A1*v1/A2"
    )
    adjudicator = _Adjudicator(NEUTRAL)

    verdict = contraction_verdicts(
        student,
        reference,
        reference.paths[0],
        adjudicator,
    )["m"]

    assert verdict.kind == FindingKind.COVERED_BY_CONTRACTION
    assert verdict.decision_channel == "symbolic_equivalent"
    assert verdict.entailment is None
    assert adjudicator.calls == []


def test_same_lhs_wrong_fragment_vetoes_false_nli_entailment(concrete_equation_case):
    student, reference = concrete_equation_case("I divided v1 by A1 to get v2 = v1/(A1*A2)")
    adjudicator = _Adjudicator(ENTAILS)

    result = grade_attempt(student, reference, contraction_adjudicator=adjudicator)

    (finding,) = _findings(result, FindingKind.NOT_DEMONSTRATED)
    assert finding.message.endswith("decision symbolic_veto")
    assert result.coverage_score == pytest.approx(2 / 3)
    assert adjudicator.calls == []


def test_word_only_equation_bridge_falls_back_to_nli(concrete_equation_case):
    student, reference = concrete_equation_case(
        "I multiplied by the first area and divided by the second area."
    )
    adjudicator = _Adjudicator(ENTAILS)

    result = grade_attempt(student, reference, contraction_adjudicator=adjudicator)

    (finding,) = _findings(result, FindingKind.COVERED_BY_CONTRACTION)
    assert finding.message.endswith("decision nli")
    assert len(adjudicator.calls) == 1


def test_unparseable_equation_fragment_falls_back_to_nli(concrete_equation_case):
    student, reference = concrete_equation_case("I rearranged to get v2 = v1/(")
    adjudicator = _Adjudicator(ENTAILS)

    verdict = contraction_verdicts(
        student,
        reference,
        reference.paths[0],
        adjudicator,
    )["m"]

    assert verdict.kind == FindingKind.COVERED_BY_CONTRACTION
    assert verdict.decision_channel == "nli"
    assert verdict.entailment == 0.96
    assert len(adjudicator.calls) == 1


def test_sign_flip_zero_form_is_symbolically_equivalent(concrete_equation_case):
    student, reference = concrete_equation_case("A1*v1/A2 = v2")
    adjudicator = _Adjudicator(NEUTRAL)

    verdict = contraction_verdicts(
        student,
        reference,
        reference.paths[0],
        adjudicator,
    )["m"]

    assert verdict.kind == FindingKind.COVERED_BY_CONTRACTION
    assert verdict.decision_channel == "symbolic_equivalent"
    assert adjudicator.calls == []


# --------------------------------------------------------------------------- #
# Conductor guard-branch coverage: the symbolic channel's parsing limits.
# --------------------------------------------------------------------------- #


def test_prose_embedded_wrong_algebra_is_vetoed() -> None:
    """The live-check hole: trailing prose around wrong algebra still vetoes."""
    decision = contraction._symbolic_equation_decision(
        _ref_equation_node("solve_for_v2", "v2 = A1*v1/A2"),
        ("I divided v1 by A1 to get v2 = v1/(A1*A2) then just used the given value",),
    )
    assert decision is False


def test_prose_word_rhs_falls_back_to_nli() -> None:
    """A trimmed bare prose word must not masquerade as algebra (no veto)."""
    decision = contraction._symbolic_equation_decision(
        _ref_equation_node("solve_for_v2", "v2 = A1*v1/A2"),
        ("v2 = the area ratio times the inlet speed",),
    )
    assert decision is None


def test_longest_parseable_run_guards() -> None:
    run = contraction._longest_parseable_run
    # token window cap keeps only the trailing/leading _MAX_SIDE_TOKENS tokens
    many = " ".join(["junk"] * 20 + ["v2"])
    assert run(many, keep="suffix") == "v2"
    # an over-long single token is skipped, leaving nothing parseable
    assert run("x" * (contraction._MAX_EXPRESSION_CHARS + 1), keep="prefix") is None
    # a candidate minting more than _MAX_SYMBOLS distinct names is skipped
    crowded = "+".join(f"a{i}" for i in range(contraction._MAX_SYMBOLS + 1))
    assert run(crowded, keep="prefix") is None
    # boolean/relational parses are not algebra; the shorter run wins instead
    assert run("x == x", keep="suffix") == "x"
    # an expression over the op cap is skipped (distinct products so sympy
    # cannot collapse the term count before count_ops sees it)
    huge = "+".join(f"s{i}*s{i + 1}" for i in range(contraction._MAX_EXPRESSION_OPS // 2 + 2))
    assert run(huge, keep="prefix") is None
    # empty side
    assert run("   ", keep="prefix") is None


def test_parse_equation_pair_guards() -> None:
    pair = contraction._parse_equation_pair
    ref = "v2 = A1*v1/A2"
    # multiple equals signs are rejected
    assert pair("a = b = c", ref) is None
    # symbol-count cap
    crowded = "+".join(f"a{i}" for i in range(contraction._MAX_SYMBOLS + 1))
    assert pair(f"{crowded} = 1", ref) is None
    # non-Expr side (sympify yields a boolean)
    assert pair("True = 1", ref) is None
    # malformed algebra raises inside sympify and is swallowed
    assert pair(")( = 1", ref) is None
    # a bare fragment symbol absent from the reference is trimmed prose
    assert pair("the = 8.0", ref) is None
    # oversized fragment (each side parseable alone, sum over the char cap)
    long_a = "x" * 300
    long_b = "y" * 300
    assert pair(f"{long_a} = {long_b}", ref) is None


def test_decision_skips_unpairable_fragment() -> None:
    """Extraction accepts two long parseable sides; the pair parser rejects
    the oversized fragment and the decision falls back to NLI (None)."""
    long_a = "x" * 300
    long_b = "y" * 300
    decision = contraction._symbolic_equation_decision(
        _ref_equation_node("solve_for_v2", "v2 = A1*v1/A2"),
        (f"{long_a} = {long_b}",),
    )
    assert decision is None


def _ref_equation_node(key: str, symbolic: str):
    from apollo.graph_compare.canonical import CanonicalNode

    return CanonicalNode(
        canonical_key=key,
        node_type="equation",
        source_node_ids=(key,),
        evidence_spans=(symbolic,),
        symbolic=symbolic,
        method=None,
        confidence=1.0,
    )
