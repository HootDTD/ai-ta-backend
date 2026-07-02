"""Unit tests for campaign.adapters (Task D3(b)).

Builds REAL artifact payloads via the actual ``apollo.grading.artifact_build``
builders (not hand-written dicts, per the task brief) and asserts the
round-trip into the exact plain-dict shapes campaign.judges (S3/S4/S5) and
campaign.report consume.
"""

from __future__ import annotations

import pytest

from apollo.grading.artifact_build import build_graph_artifact, build_llm_artifact
from apollo.grading.audited_grade import AuditedGrade
from apollo.grading.composite import load_weights
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.resolution.result import ResolutionResult, ResolvedNode
from campaign import adapters
from campaign.cast.personas.schema import ExpectedLedger, PersonaAttempt

pytestmark = pytest.mark.unit


def _grade(findings: tuple[Finding, ...]) -> GradeResult:
    return GradeResult(
        coverage_score=0.6,
        soundness_score=0.6,
        bisimilarity_score=0.6,
        node_coverage_score=0.6,
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


def _findings() -> tuple[Finding, ...]:
    return (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="def.pressure_velocity_tradeoff",
            student_node_ids=("n_a",),
            evidence_spans=("faster flow means lower pressure",),
            confidence=0.92,
        ),
        Finding(kind=FindingKind.MISSING_NODE, canonical_key="eq.bernoulli_full", score=0.0),
        Finding(
            kind=FindingKind.CONTRADICTION,
            canonical_key="misc.pressure_velocity_same_direction",
            student_node_ids=("n_m",),
            evidence_spans=("pressure goes up when it speeds up",),
            score=0.0,
        ),
        Finding(
            kind=FindingKind.UNRESOLVED,
            student_node_ids=("n_x",),
            evidence_spans=("gibberish"),
        ),
        Finding(kind=FindingKind.MATCHED_EDGE, message="def.pressure_velocity_tradeoff -PRECEDES-> eq.bernoulli_full (explicit)"),
    )


def _resolution() -> ResolutionResult:
    return ResolutionResult(
        resolved=(
            ResolvedNode(
                node_id="n_a",
                resolution="resolved",
                resolved_key="def.pressure_velocity_tradeoff",
                resolved_canon_key=1,
                method="alias",
                confidence=0.92,
            ),
            ResolvedNode(
                node_id="n_m",
                resolution="resolved",
                resolved_key="misc.pressure_velocity_same_direction",
                resolved_canon_key=2,
                method="fuzzy",
                confidence=0.80,
            ),
            ResolvedNode(
                node_id="n_x",
                resolution="unresolved",
                resolved_key=None,
                resolved_canon_key=None,
                method="unresolved",
                confidence=0.0,
            ),
        ),
        tier_counts={"alias": 1, "fuzzy": 1, "unresolved": 1},
        llm_calls=0,
    )


@pytest.fixture
def shadow():
    findings = _findings()
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
        opposes_map={"misc.pressure_velocity_same_direction": "def.pressure_velocity_tradeoff"},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=_resolution(),
    )


@pytest.fixture
def graph_artifact(shadow) -> dict:
    return build_graph_artifact(
        shadow=shadow, weights=load_weights(), clarification_trace=[
            {"probe_question": "Do you mean pressure or velocity?",
             "clarification_text": "pressure", "credit": "granted"},
        ], latency_ms=1234
    )


@pytest.fixture
def llm_artifact() -> dict:
    return build_llm_artifact(
        coverage={"covered": ["def.pressure_velocity_tradeoff"], "missing": ["eq.bernoulli_full"]},
        rubric={"overall": {"score": 71}},
        weights=load_weights(),
        graph_failure=None,
        latency_ms=999,
    )


@pytest.fixture
def persona() -> PersonaAttempt:
    return PersonaAttempt(
        persona="misconception",
        subject="fluid_mechanics",
        concept="bernoulli_principle",
        problem_id="bernoulli_full_find_p2",
        system_prompt="teach bernoulli",
        scripted_beats=["explain pressure-velocity tradeoff"],
        clarification_policy="answer_correctly",
        expected=ExpectedLedger(
            credited=["def.pressure_velocity_tradeoff"],
            unresolved=["eq.bernoulli_full"],
            misconceptions=["misc.pressure_velocity_same_direction"],
        ),
    )


_TRANSCRIPT = [
    {"role": "student", "content": "faster flow means lower pressure"},
    {"role": "apollo", "content": "Wait, do you mean pressure or velocity?"},
    {"role": "student", "content": "pressure"},
]


# --- subject_kind_for / all_subject_kinds -----------------------------------


def test_subject_kind_for_seeded():
    assert adapters.subject_kind_for("fluid_mechanics") == "seeded"
    assert adapters.subject_kind_for("macroeconomics") == "seeded"


def test_subject_kind_for_wu_aas_and_held_out():
    assert adapters.subject_kind_for("linear_motion") == "wu_aas"
    assert adapters.subject_kind_for("held_out_subject") == "held_out"


def test_subject_kind_for_unknown():
    assert adapters.subject_kind_for("nonexistent") == "unknown"


def test_all_subject_kinds_covers_every_registered_subject():
    kinds = adapters.all_subject_kinds()
    assert kinds == {
        "fluid_mechanics": "seeded",
        "macroeconomics": "seeded",
        "linear_motion": "wu_aas",
        "held_out_subject": "held_out",
    }


# --- transcript_to_text ------------------------------------------------------


def test_transcript_to_text_renders_role_content_lines():
    text = adapters.transcript_to_text(_TRANSCRIPT)
    assert text == (
        "student: faster flow means lower pressure\n"
        "apollo: Wait, do you mean pressure or velocity?\n"
        "student: pressure"
    )


def test_transcript_to_text_empty():
    assert adapters.transcript_to_text([]) == ""


# --- ledger_entry_to_judge_shape --------------------------------------------


def test_ledger_entry_to_judge_shape_renames_canonical_key():
    entry = {"canonical_key": "eq.a", "status": "credited", "evidence_span": "x"}
    out = adapters.ledger_entry_to_judge_shape(entry)
    assert out["key"] == "eq.a"
    assert "canonical_key" not in out
    assert out["status"] == "credited"
    assert out["evidence_span"] == "x"


def test_node_ledger_to_judge_shape_maps_every_entry(graph_artifact):
    out = adapters.node_ledger_to_judge_shape(graph_artifact["node_ledger"])
    assert len(out) == len(graph_artifact["node_ledger"])
    assert all("key" in e and "canonical_key" not in e for e in out)


# --- attempt_to_s3_item -------------------------------------------------------


def test_attempt_to_s3_item_shape_matches_judge_contract(graph_artifact, persona):
    item = adapters.attempt_to_s3_item(
        attempt_id=42, transcript=_TRANSCRIPT, artifact=graph_artifact, expected=persona.expected
    )
    assert item["attempt_id"] == 42
    assert "faster flow means lower pressure" in item["transcript"]
    assert item["expected"] == {
        "credited": ["def.pressure_velocity_tradeoff"],
        "unresolved": ["eq.bernoulli_full"],
        "misconceptions": ["misc.pressure_velocity_same_direction"],
    }
    keys = {e["key"] for e in item["node_ledger"]}
    assert "def.pressure_velocity_tradeoff" in keys
    assert "eq.bernoulli_full" in keys

    # Round-trips cleanly through the REAL S3 judge's pure diff function.
    from campaign.judges.s3_student_fidelity import S3StudentFidelityJudge, ledger_vs_expected

    diff = ledger_vs_expected(item["node_ledger"], item["expected"])
    assert diff["credited"]["matched"] == ["def.pressure_velocity_tradeoff"]
    assert diff["misconceptions"]["matched"] == ["misc.pressure_velocity_same_direction"]

    built_items = S3StudentFidelityJudge(llm=None).build_items([item])
    assert len(built_items) == len(item["node_ledger"])
    for built in built_items:
        assert built["item_id"] == f"42:{built['key']}"


# --- misconception_bank_lookup / attempt_to_s5_item -------------------------


def test_misconception_bank_lookup_real_bank():
    bank = adapters.misconception_bank_lookup("fluid_mechanics", "bernoulli_principle")
    assert "misc.pressure_velocity_same_direction" in bank
    assert bank["misc.pressure_velocity_same_direction"]


def test_misconception_bank_lookup_missing_concept_returns_empty():
    assert adapters.misconception_bank_lookup("fluid_mechanics", "not_a_concept") == {}


def test_attempt_to_s5_item_carries_bank_description(graph_artifact, persona):
    item = adapters.attempt_to_s5_item(
        attempt_id=7,
        artifact=graph_artifact,
        expected=persona.expected,
        subject=persona.subject,
        concept=persona.concept,
    )
    assert item["attempt_id"] == 7
    assert len(item["asserted_misconceptions"]) == 1
    m = item["asserted_misconceptions"][0]
    assert m["key"] == "misc.pressure_velocity_same_direction"
    assert m["utterance"] == "pressure goes up when it speeds up"
    assert m["bank_description"]

    from campaign.judges.s5_misconceptions import S5MisconceptionJudge, misconception_recall

    built = S5MisconceptionJudge(llm=None).build_items([item])
    assert built[0]["item_id"] == "7:misc.pressure_velocity_same_direction"
    recall = misconception_recall([item])
    assert recall["overall_recall"] == 1.0


# --- extract_apollo_questions / clarification_trace / attempt_to_s4_item ---


def test_extract_apollo_questions_filters_question_marks():
    qs = adapters.extract_apollo_questions(_TRANSCRIPT)
    assert qs == ["Wait, do you mean pressure or velocity?"]


def test_clarification_trace_to_judge_shape():
    out = adapters.clarification_trace_to_judge_shape(
        [{"probe_question": "q1", "clarification_text": "a1", "credit": "granted"}]
    )
    assert out == [{"question": "q1", "answer": "a1", "credit": "granted"}]


def test_attempt_to_s4_item_shape(graph_artifact):
    item = adapters.attempt_to_s4_item(attempt_id=9, transcript=_TRANSCRIPT, artifact=graph_artifact)
    assert item["attempt_id"] == 9
    assert item["apollo_questions"] == ["Wait, do you mean pressure or velocity?"]
    assert item["clarification_trace"] == [
        {"question": "Do you mean pressure or velocity?", "answer": "pressure", "credit": "granted"}
    ]
    assert "eq.bernoulli_full" in item["unresolved_keys"]
    assert "misc.pressure_velocity_same_direction" in item["misconception_keys"]

    from campaign.judges.s4_apollo_coherence import S4ApolloCoherenceJudge

    built = S4ApolloCoherenceJudge(llm=None).build_items([item])
    assert built[0]["item_id"] == "9"


# --- graph_payload_for / llm_payload_for / attempt_to_report_record --------


def test_graph_payload_for_finds_graph_row_in_either_slot(graph_artifact, llm_artifact):
    assert adapters.graph_payload_for(artifact_canonical=graph_artifact, artifact_pair=llm_artifact) == graph_artifact
    assert adapters.graph_payload_for(artifact_canonical=llm_artifact, artifact_pair=graph_artifact) == graph_artifact
    assert adapters.graph_payload_for(artifact_canonical=llm_artifact, artifact_pair=None) is None


def test_llm_payload_for_finds_llm_row_in_either_slot(graph_artifact, llm_artifact):
    assert adapters.llm_payload_for(artifact_canonical=graph_artifact, artifact_pair=llm_artifact) == llm_artifact
    assert adapters.llm_payload_for(artifact_canonical=llm_artifact, artifact_pair=graph_artifact) == llm_artifact
    assert adapters.llm_payload_for(artifact_canonical=graph_artifact, artifact_pair=None) is None


def test_attempt_to_report_record_paired_shadow_mode(graph_artifact, llm_artifact):
    # Shadow-mode tuning run: canonical=llm (served), pair=graph (shadow).
    record = adapters.attempt_to_report_record(
        attempt_id=99, subject="fluid_mechanics", artifact_canonical=llm_artifact, artifact_pair=graph_artifact
    )
    assert record["attempt_id"] == 99
    assert record["subject"] == "fluid_mechanics"
    assert record["band"] is not None
    assert record["grading_latency_ms"] == llm_artifact["grading_latency_ms"]
    assert record["shadow_succeeded"] is True
    assert record["shadow_abstained"] is False
    assert record["graph_composite"] == graph_artifact["scores"]["composite"]
    assert record["llm_composite"] == llm_artifact["scores"]["composite"]

    # Round-trips through the REAL report gate math.
    from campaign.report import band_for_score, graph_graded_fraction

    assert graph_graded_fraction([record]) == 1.0
    assert band_for_score(record["graph_composite"]) in {
        "Strong", "Proficient", "Developing", "Beginning"
    }


def test_attempt_to_report_record_no_shadow_ran():
    record = adapters.attempt_to_report_record(
        attempt_id=1, subject="macroeconomics", artifact_canonical=None, artifact_pair=None
    )
    assert record == {
        "attempt_id": 1,
        "subject": "macroeconomics",
        "band": None,
        "grading_latency_ms": None,
        "shadow_succeeded": False,
        "shadow_abstained": False,
        "graph_composite": None,
        "llm_composite": None,
    }


def test_attempt_to_report_record_graph_abstained():
    graph_abstained = {
        "grader_used": "graph",
        "scores": {"composite": 0.4},
        "abstention": {"abstained": True},
        "grading_latency_ms": 500,
    }
    llm_served = {"grader_used": "llm_fallback", "scores": {"composite": 0.6}, "node_ledger": []}
    record = adapters.attempt_to_report_record(
        attempt_id=2, subject="fluid_mechanics", artifact_canonical=llm_served, artifact_pair=graph_abstained
    )
    assert record["shadow_succeeded"] is True
    assert record["shadow_abstained"] is True
    assert record["graph_composite"] == 0.4
    assert record["llm_composite"] == 0.6
