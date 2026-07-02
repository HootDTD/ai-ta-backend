"""Unit tests for the gate evaluation report generator (Plan Task E3).

Everything runs against synthetic fixture data — no DB, no Neo4j, no LLM, no
Docker. ``JudgeResult``/``Verdict`` objects are constructed directly (the same
contract E1's own tests use); attempt records and adjudication verdicts are
plain dicts shaped per ``campaign/report.py``'s module docstring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from campaign.config import CampaignConfig
from campaign.judges.base import JudgeResult, Verdict
from campaign.report import (
    ADJUDICATION_SANE_BAR,
    GRAPH_GRADED_BAR,
    LATENCY_P95_MS_BAR,
    MIN_SUBJECTS,
    S1_BAR,
    S4_BAR,
    adjudication_gate,
    band_for_score,
    breadth_gate,
    build_report,
    classify_subject,
    graph_graded_fraction,
    graph_graded_gate,
    latency_p95_ms,
    ops_gate,
    paired_comparison,
    render_markdown,
    stage_gate,
    write_report,
)


def _judge_result(stage: str, *, ok: int, total: int, extra=None) -> JudgeResult:
    verdicts = tuple(Verdict(f"{stage}:{i}", i < ok, "") for i in range(total))
    passed = sum(1 for v in verdicts if v.ok)
    pass_rate = (passed / total) if total else 0.0
    return JudgeResult(
        stage=stage,
        verdicts=verdicts,
        passed=passed,
        total=total,
        pass_rate=pass_rate,
        extra=extra or {},
    )


def _passing_judge_results() -> dict[str, JudgeResult]:
    return {
        "s1_reference_graph": _judge_result("s1_reference_graph", ok=20, total=20),
        "s2_ingestion": _judge_result("s2_ingestion", ok=10, total=10),
        "s3_student_fidelity": _judge_result("s3_student_fidelity", ok=19, total=20),
        "s4_apollo_coherence": _judge_result("s4_apollo_coherence", ok=9, total=10),
        "s5_misconceptions": _judge_result("s5_misconceptions", ok=9, total=10),
    }


def _attempt(**overrides) -> dict:
    base = dict(
        attempt_id="a1",
        subject="fluid_mechanics",
        band="Strong",
        grading_latency_ms=1000,
        shadow_succeeded=True,
        shadow_abstained=False,
        graph_composite=0.9,
        llm_composite=0.85,
    )
    base.update(overrides)
    return base


def _sample_config() -> CampaignConfig:
    return CampaignConfig(
        axis_weights={"a": 1.0},
        letter_bands=((90, "A"),),
        nli_model="cross-encoder/nli-deberta-v3-large",
        nli_params=CampaignConfig.capture_live().nli_params,
        abstention_thresholds={"unresolved_rate": 0.5},
        flags={"APOLLO_NLI_ENABLED": True},
    )


# --- band_for_score ----------------------------------------------------------


def test_band_for_score_edges():
    assert band_for_score(0.85) == "Strong"
    assert band_for_score(0.8499) == "Proficient"
    assert band_for_score(0.70) == "Proficient"
    assert band_for_score(0.6999) == "Developing"
    assert band_for_score(0.0) == "Beginning"


def test_band_for_score_empty_bands_returns_unknown():
    assert band_for_score(0.5, bands=()) == "Unknown"


# --- stage_gate ----------------------------------------------------------


def test_stage_gate_pass_and_fail_bars():
    passing = stage_gate(_judge_result("s1_reference_graph", ok=95, total=100))
    assert passing.passed
    assert passing.bar == S1_BAR

    failing = stage_gate(_judge_result("s4_apollo_coherence", ok=8, total=10))
    assert not failing.passed
    assert failing.bar == S4_BAR


def test_stage_gate_zero_items_fails():
    outcome = stage_gate(_judge_result("s1_reference_graph", ok=0, total=0))
    assert not outcome.passed
    assert "zero items" in outcome.detail


def test_stage_gate_unknown_stage_requires_explicit_bar():
    result = _judge_result("s9_unknown", ok=1, total=1)
    with pytest.raises(ValueError):
        stage_gate(result)
    outcome = stage_gate(result, bar=0.5)
    assert outcome.passed


# --- adjudication_gate ----------------------------------------------------


def test_adjudication_gate_passes_above_bar_zero_harmful():
    verdicts = [{"attempt_id": str(i), "verdict": "sane", "reason": ""} for i in range(19)]
    verdicts.append({"attempt_id": "x", "verdict": "not_sane", "reason": "meh"})
    outcome = adjudication_gate(verdicts)
    assert outcome.passed
    assert outcome.value == pytest.approx(0.95)


def test_adjudication_gate_fails_on_any_harmful_even_if_sane_rate_high():
    verdicts = [{"attempt_id": str(i), "verdict": "sane"} for i in range(99)]
    verdicts.append({"attempt_id": "bad", "verdict": "not_sane_harmful"})
    outcome = adjudication_gate(verdicts)
    assert not outcome.passed


def test_adjudication_gate_empty_sample_fails():
    outcome = adjudication_gate([])
    assert not outcome.passed
    assert outcome.value == 0.0


def test_adjudication_gate_bar_is_named_constant_default():
    assert adjudication_gate([{"verdict": "sane"}]).bar == ADJUDICATION_SANE_BAR


# --- graph_graded_fraction / gate ------------------------------------------


def test_graph_graded_fraction_counterfactual_shadow_mode():
    attempts = [
        _attempt(shadow_succeeded=True, shadow_abstained=False),
        _attempt(shadow_succeeded=True, shadow_abstained=True),  # abstained -> not graded
        _attempt(shadow_succeeded=False, shadow_abstained=False),  # graph exception -> not graded
    ]
    assert graph_graded_fraction(attempts) == pytest.approx(1 / 3)


def test_graph_graded_fraction_empty_is_zero():
    assert graph_graded_fraction([]) == 0.0


def test_graph_graded_gate_per_subject():
    attempts = [
        _attempt(subject="fluid_mechanics", shadow_succeeded=True, shadow_abstained=False),
        _attempt(subject="fluid_mechanics", shadow_succeeded=True, shadow_abstained=False),
        _attempt(subject="macroeconomics", shadow_succeeded=False, shadow_abstained=False),
    ]
    outcomes = graph_graded_gate(attempts)
    assert outcomes["fluid_mechanics"].passed
    assert outcomes["fluid_mechanics"].value == 1.0
    assert not outcomes["macroeconomics"].passed
    assert outcomes["macroeconomics"].bar == GRAPH_GRADED_BAR


# --- latency / ops ----------------------------------------------------------


def test_latency_p95_excludes_nulls():
    attempts = [_attempt(grading_latency_ms=v) for v in [100, 200, None, 300]]
    p95 = latency_p95_ms(attempts)
    assert p95 is not None


def test_latency_p95_none_when_no_latencies():
    assert latency_p95_ms([_attempt(grading_latency_ms=None)]) is None


def test_latency_p95_single_value():
    assert latency_p95_ms([_attempt(grading_latency_ms=500)]) == 500


def test_ops_gate_passes_under_bar_and_no_stalls():
    attempts = [_attempt(grading_latency_ms=1000) for _ in range(5)]
    outcome = ops_gate(attempts)
    assert outcome.passed
    assert outcome.bar == LATENCY_P95_MS_BAR


def test_ops_gate_fails_on_stall_warning_even_if_latency_ok():
    attempts = [_attempt(grading_latency_ms=1000)]
    outcome = ops_gate(attempts, event_loop_stall_warnings=["slow callback took 0.5s"])
    assert not outcome.passed


def test_ops_gate_fails_when_no_latencies_recorded():
    outcome = ops_gate([_attempt(grading_latency_ms=None)])
    assert not outcome.passed
    assert "n/a" in outcome.detail


def test_ops_gate_fails_over_bar():
    outcome = ops_gate([_attempt(grading_latency_ms=20_000)])
    assert not outcome.passed


# --- breadth ----------------------------------------------------------------


def test_breadth_gate_passes_with_four_subjects_incl_wu_aas_and_held_out():
    attempts = [
        _attempt(subject="fluid_mechanics"),
        _attempt(subject="macroeconomics"),
        _attempt(subject="linear_motion"),
        _attempt(subject="held_out_subject"),
    ]
    kinds = {
        "fluid_mechanics": "seeded",
        "macroeconomics": "seeded",
        "linear_motion": "wu_aas",
        "held_out_subject": "held_out",
    }
    outcome = breadth_gate(attempts, subject_kinds=kinds)
    assert outcome.passed
    assert outcome.value == 4.0
    assert outcome.bar == float(MIN_SUBJECTS)


def test_breadth_gate_fails_without_held_out():
    attempts = [
        _attempt(subject="fluid_mechanics"),
        _attempt(subject="macroeconomics"),
        _attempt(subject="linear_motion"),
        _attempt(subject="extra_subject"),
    ]
    kinds = {
        "fluid_mechanics": "seeded",
        "macroeconomics": "seeded",
        "linear_motion": "wu_aas",
        "extra_subject": "seeded",
    }
    outcome = breadth_gate(attempts, subject_kinds=kinds)
    assert not outcome.passed


def test_breadth_gate_fails_below_min_subjects():
    attempts = [_attempt(subject="fluid_mechanics")]
    outcome = breadth_gate(attempts, subject_kinds={"fluid_mechanics": "wu_aas"})
    assert not outcome.passed


def test_classify_subject_unknown_without_kinds():
    assert classify_subject("mystery") == "unknown"
    assert classify_subject("mystery", {"mystery": "held_out"}) == "held_out"


# --- paired_comparison -------------------------------------------------------


def test_paired_comparison_band_agreement_and_delta():
    attempts = [
        _attempt(attempt_id="a1", graph_composite=0.9, llm_composite=0.88),  # both Strong
        _attempt(attempt_id="a2", graph_composite=0.6, llm_composite=0.9),  # Developing vs Strong
    ]
    result = paired_comparison(attempts)
    assert result["n_pairs"] == 2
    assert result["band_agreement_rate"] == 0.5
    assert result["mean_delta"] == pytest.approx(((0.9 - 0.88) + (0.6 - 0.9)) / 2)
    assert result["top_divergent"][0]["attempt_id"] == "a2"


def test_paired_comparison_skips_missing_pairs():
    attempts = [_attempt(graph_composite=None), _attempt(llm_composite=None)]
    result = paired_comparison(attempts)
    assert result["n_pairs"] == 0
    assert result["skipped_missing_pair"] == 2
    assert result["band_agreement_rate"] == 0.0
    assert result["top_divergent"] == []


def test_paired_comparison_top_divergent_capped_at_ten():
    attempts = [
        _attempt(attempt_id=str(i), graph_composite=0.9, llm_composite=0.9 - i * 0.01)
        for i in range(15)
    ]
    result = paired_comparison(attempts)
    assert len(result["top_divergent"]) == 10
    # Largest deltas first.
    assert result["top_divergent"][0]["attempt_id"] == "14"


# --- build_report / render_markdown / write_report --------------------------


def _full_passing_setup():
    judge_results = _passing_judge_results()
    attempts = (
        [
            _attempt(
                attempt_id=f"fm-{i}",
                subject="fluid_mechanics",
                shadow_succeeded=True,
                shadow_abstained=False,
                graph_composite=0.9,
                llm_composite=0.85,
                grading_latency_ms=1000,
            )
            for i in range(8)
        ]
        + [
            _attempt(
                attempt_id=f"mac-{i}",
                subject="macroeconomics",
                shadow_succeeded=True,
                shadow_abstained=False,
                graph_composite=0.8,
                llm_composite=0.78,
                grading_latency_ms=1200,
            )
            for i in range(8)
        ]
        + [
            _attempt(
                attempt_id=f"lm-{i}",
                subject="linear_motion",
                shadow_succeeded=True,
                shadow_abstained=False,
                graph_composite=0.75,
                llm_composite=0.7,
                grading_latency_ms=1500,
            )
            for i in range(8)
        ]
        + [
            _attempt(
                attempt_id=f"ho-{i}",
                subject="held_out_subject",
                shadow_succeeded=True,
                shadow_abstained=False,
                graph_composite=0.72,
                llm_composite=0.7,
                grading_latency_ms=1400,
            )
            for i in range(8)
        ]
    )
    adjudication_verdicts = [{"attempt_id": f"a{i}", "verdict": "sane"} for i in range(20)]
    subject_kinds = {
        "fluid_mechanics": "seeded",
        "macroeconomics": "seeded",
        "linear_motion": "wu_aas",
        "held_out_subject": "held_out",
    }
    return judge_results, attempts, adjudication_verdicts, subject_kinds


def test_build_report_all_gates_pass_on_healthy_fixture():
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="deadbeef",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    assert report.passed, [g.detail for g in report.failures]
    assert report.failures == ()
    names = {g.name for g in report.gates}
    assert "s1_reference_graph" in names
    assert "graph_graded:fluid_mechanics" in names
    assert "adjudication" in names
    assert "ops" in names
    assert "breadth" in names


def test_build_report_missing_judge_result_fails_that_stage():
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    del judge_results["s5_misconceptions"]
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="sha",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    assert not report.passed
    s5 = next(g for g in report.gates if g.name == "s5_misconceptions")
    assert not s5.passed
    assert "no judge result supplied" in s5.detail


def test_build_report_fails_on_stall_warning():
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="sha",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
        event_loop_stall_warnings=["slow callback"],
    )
    assert not report.passed
    ops = next(g for g in report.gates if g.name == "ops")
    assert not ops.passed


def test_build_report_evidence_carries_config_snapshot_and_counts():
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="sha",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    assert report.evidence["n_attempts"] == len(attempts)
    assert report.evidence["n_adjudication_samples"] == len(adjudication)
    assert report.evidence["config_snapshot"]["axis_weights"] == {"a": 1.0}
    assert "fluid_mechanics" in report.evidence["graph_graded_by_subject"]


def test_render_markdown_contains_overall_verdict_and_gate_table():
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    report = build_report(
        run_id="run-42",
        config=_sample_config(),
        config_sha="sha123",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    md = render_markdown(report)
    assert "run-42" in md
    assert "sha123" in md
    assert "PASS" in md
    assert "| s1_reference_graph |" in md
    assert "Paired graph-vs-LLM comparison" in md
    assert "Band agreement rate (primary paired metric)" in md
    assert "informational / cross-scale only" in md


def test_render_markdown_lists_failures_as_work_queue():
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    del judge_results["s4_apollo_coherence"]
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="sha",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    md = render_markdown(report)
    assert "Failures (next work queue)" in md
    assert "s4_apollo_coherence" in md


def test_render_markdown_no_paired_data_omits_divergent_table():
    judge_results, _attempts, adjudication, subject_kinds = _full_passing_setup()
    unpaired_attempts = [_attempt(graph_composite=None, llm_composite=None)]
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="sha",
        judge_results=judge_results,
        attempts=unpaired_attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    md = render_markdown(report)
    assert "Pairs compared: 0" in md
    assert "| Attempt |" not in md


def test_write_report_creates_markdown_and_json(tmp_path: Path):
    judge_results, attempts, adjudication, subject_kinds = _full_passing_setup()
    report = build_report(
        run_id="run-1",
        config=_sample_config(),
        config_sha="sha",
        judge_results=judge_results,
        attempts=attempts,
        adjudication_verdicts=adjudication,
        subject_kinds=subject_kinds,
    )
    out_dir = tmp_path / "out" / "run-1"
    md_path, json_path = write_report(report, out_dir)
    assert md_path.exists()
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-1"
    assert data["passed"] is True
    assert len(data["gates"]) == len(report.gates)
