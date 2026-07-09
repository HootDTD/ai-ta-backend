"""Tests for the Phase-1 misconception-detector diagnostic trace.

Design spec: ``docs/_archive/specs/2026-07-09-apollo-misconception-trace-and-
tau-calibration-design.md`` (Phase 1). Recall-gap handoff:
``docs/_archive/handoffs/2026-07-08-apollo-misconception-recall-gap-handoff.md``.

The trace is INSTRUMENTATION ONLY — it re-derives, per reference-graph node,
what the judge said and which §5 truth-table row fired, read-only from the same
artifacts the live chain produced. These tests assert:

  * the documented per-node payload SHAPE + fields (flag-ON contract),
  * the gate-row classification for every truth-table row that matters for the
    recall gap (co-key row 3 / lone-solo row 5 / clarify / drop),
  * the per-attempt false-Strong roll-up (misconception-class vs control),
  * the JSONL emitter (write + machine-parseable + soft-fail),
  * the ``APOLLO_MISC_TRACE`` flag + ``APOLLO_MISC_TRACE_PATH`` config seam,
    default OFF / default path.
"""

from __future__ import annotations

import json

from apollo.ontology import KGGraph, build_node
from apollo.overseer.misconception_detector import config as detector_config
from apollo.overseer.misconception_detector import trace as trace_mod
from apollo.overseer.misconception_detector.gate import gate_findings
from apollo.overseer.misconception_detector.trace import (
    build_node_traces,
    emit_traces,
    is_false_strong,
    trace_attempt,
)
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
    MergeOutcome,
)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _def_node(node_id: str) -> object:
    return build_node(
        node_type="definition",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"concept": node_id, "meaning": f"meaning of {node_id}"},
    )


def _graph(*node_ids: str) -> KGGraph:
    return KGGraph(nodes=[_def_node(nid) for nid in node_ids], edges=[])


def _judge(
    *,
    concept_key: str,
    confidence: float,
    bank_code: str | None,
    verdict: str = "misconception",
    verdict_token_prob_present: bool = True,
    evidence_span: str = "student said X",
) -> ConceptFinding:
    signature = f"misc.{bank_code}" if bank_code else f"unkeyed:{concept_key}"
    return ConceptFinding(
        concept_key=concept_key,
        verdict=verdict,
        confidence=confidence,
        severity=0.0,
        evidence_span=evidence_span,
        signature=signature,
        source="judge",
        corroborated=False,
        verdict_token_prob_present=verdict_token_prob_present,
        bank_code=bank_code,
    )


def _bank(*, bank_code: str, confidence: float, above_floor: bool) -> ConceptFinding:
    return ConceptFinding(
        concept_key="42",  # str(concept_id) — the real cross-namespace key
        verdict="misconception",
        confidence=confidence,
        severity=0.0,
        evidence_span="raw utterance",
        signature=f"misc.{bank_code}",
        source="bank_pattern",
        corroborated=False,
        bank_code=bank_code,
        bank_match_above_floor=above_floor,
    )


def _sympy(*, concept_key: str, bank_code: str) -> ConceptFinding:
    return ConceptFinding(
        concept_key=concept_key,
        verdict="misconception",
        confidence=1.0,
        severity=0.0,
        evidence_span="equation sign flip",
        signature=f"misc.{bank_code}",
        source="sympy_veto",
        corroborated=False,
        bank_code=bank_code,
    )


def _outcome(*, penalty: float = 0.0, ceiling: bool = False) -> MergeOutcome:
    return MergeOutcome(
        misconception_penalty=penalty,
        misconceptions=(),
        ceiling_applied=ceiling,
        ledger_findings=(),
    )


def _run(findings: tuple[ConceptFinding, ...], graph: KGGraph, **kw):
    """Gate the findings the SAME way the live chain does, then build rows so
    each test observes the trace against the real gate output (no re-implemented
    gate)."""
    gated = gate_findings(findings)
    return build_node_traces(
        attempt_id=kw.get("attempt_id", 88),
        reference_graph=graph,
        detection=DetectionResult(per_concept=findings),
        gated=gated,
        outcome=kw.get("outcome", _outcome()),
        centrality=kw.get("centrality", {}),
        final_band=kw.get("final_band", "Strong"),
        is_false_strong=kw.get("is_false_strong", False),
    )


# --------------------------------------------------------------------------- #
# Payload shape (flag-ON contract)
# --------------------------------------------------------------------------- #
def test_row_has_all_documented_fields():
    graph = _graph("node.real_basis")
    judge = _judge(
        concept_key="node.real_basis", confidence=0.95, bank_code="nominal_for_real"
    )
    bank = _bank(bank_code="nominal_for_real", confidence=0.582, above_floor=False)

    rows = _run((judge, bank), graph, centrality={"node.real_basis": 0.7})

    assert len(rows) == 1
    row = rows[0]
    expected_top_level = {
        "attempt_id",
        "node_id",
        "node_type",
        "judge",
        "finding_signature",
        "bank_code",
        "bank_pattern_top1",
        "cokey_bank_code",
        "centrality",
        "gate_decision",
        "gate_row",
        "ceiling_eligible",
        "final_band",
        "misconception_penalty",
        "ceiling_applied",
        "is_false_strong",
    }
    assert set(row.keys()) == expected_top_level
    # Judge sub-object shape.
    assert set(row["judge"].keys()) == {
        "verdict",
        "misconception_code",
        "confidence",
        "verdict_token_prob_present",
    }
    assert row["judge"]["verdict"] == "misconception"
    assert row["judge"]["misconception_code"] == "nominal_for_real"
    assert row["judge"]["confidence"] == 0.95
    assert row["judge"]["verdict_token_prob_present"] is True
    # bank_pattern top-1 sub-object shape (the below-floor best match, handoff §3).
    assert set(row["bank_pattern_top1"].keys()) == {
        "bank_code",
        "similarity",
        "above_floor",
    }
    assert row["bank_pattern_top1"]["similarity"] == 0.582
    assert row["bank_pattern_top1"]["above_floor"] is False
    assert row["node_id"] == "node.real_basis"
    assert row["node_type"] == "definition"
    assert row["attempt_id"] == 88
    assert row["centrality"] == 0.7


def test_verbalized_path_bit_is_reported():
    """The verdict_token_prob_present bit (T1/T3 diagnostic) is surfaced."""
    graph = _graph("n")
    judge = _judge(
        concept_key="n",
        confidence=0.88,
        bank_code="c",
        verdict_token_prob_present=False,
    )
    rows = _run((judge,), graph)
    assert rows[0]["judge"]["verdict_token_prob_present"] is False


# --------------------------------------------------------------------------- #
# Gate-row classification (the recall-gap-critical rows)
# --------------------------------------------------------------------------- #
def test_row3_cokey_dock():
    graph = _graph("node.real_basis")
    judge = _judge(
        concept_key="node.real_basis", confidence=0.95, bank_code="nominal_for_real"
    )
    bank = _bank(bank_code="nominal_for_real", confidence=0.582, above_floor=False)

    rows = _run((judge, bank), graph)

    assert rows[0]["gate_row"] == "row3_cokey_dock"
    assert rows[0]["gate_decision"] == "dock"
    assert rows[0]["cokey_bank_code"] == "nominal_for_real"
    assert rows[0]["ceiling_eligible"] is True


def test_row3b_cokey_clarify_when_judge_sub_routed_tau():
    graph = _graph("node.real_basis")
    judge = _judge(
        concept_key="node.real_basis", confidence=0.50, bank_code="nominal_for_real"
    )
    bank = _bank(bank_code="nominal_for_real", confidence=0.582, above_floor=False)

    rows = _run((judge, bank), graph)

    assert rows[0]["gate_row"] == "row3b_cokey_clarify"
    assert rows[0]["gate_decision"] == "needs_clarification"


def test_row5_lone_solo_dock_penalty_only():
    graph = _graph("node.real_basis")
    judge = _judge(
        concept_key="node.real_basis", confidence=0.95, bank_code="nominal_for_real"
    )

    rows = _run((judge,), graph)

    assert rows[0]["gate_row"] == "row5_lone_solo_dock"
    assert rows[0]["gate_decision"] == "dock"
    assert rows[0]["ceiling_eligible"] is False
    assert rows[0]["cokey_bank_code"] is None


def test_row6_keyed_sub_solo_clarify():
    graph = _graph("n")
    judge = _judge(
        concept_key="n", confidence=0.86, bank_code="c"
    )  # >=TAU_FIRE, <TAU_SOLO

    rows = _run((judge,), graph)

    assert rows[0]["gate_row"] == "row6_keyed_sub_solo_clarify"
    assert rows[0]["gate_decision"] == "needs_clarification"


def test_row7_unkeyed_clarify():
    graph = _graph("n")
    judge = _judge(concept_key="n", confidence=0.99, bank_code=None)

    rows = _run((judge,), graph)

    assert rows[0]["gate_row"] == "row7_unkeyed_clarify"
    assert rows[0]["gate_decision"] == "needs_clarification"
    assert rows[0]["finding_signature"] == "unkeyed:n"
    assert rows[0]["bank_code"] is None


def test_row8_keyed_sub_routed_drop():
    graph = _graph("n")
    judge = _judge(concept_key="n", confidence=0.50, bank_code="c")

    rows = _run((judge,), graph)

    assert rows[0]["gate_row"] == "row8_keyed_sub_routed_drop"
    assert rows[0]["gate_decision"] == "drop"


def test_row8_unkeyed_drop():
    graph = _graph("n")
    judge = _judge(concept_key="n", confidence=0.10, bank_code=None)

    rows = _run((judge,), graph)

    assert rows[0]["gate_row"] == "row8_unkeyed_drop"
    assert rows[0]["gate_decision"] == "drop"


def test_row1_2_sympy_dock():
    graph = _graph("node.eq")
    veto = _sympy(concept_key="node.eq", bank_code="sign_flip")

    rows = _run((veto,), graph)

    assert rows[0]["gate_row"] == "row1_2_sympy"
    assert rows[0]["gate_decision"] == "dock"
    assert rows[0]["ceiling_eligible"] is True


def test_node_with_no_judge_is_dropped_with_null_judge():
    """A reference node the judge produced nothing for is still traced (full
    node inventory), decision drop, judge None."""
    graph = _graph("node.untouched")
    judge = _judge(concept_key="node.other", confidence=0.95, bank_code="c")

    rows = _run((judge,), graph)

    assert len(rows) == 1
    row = rows[0]
    assert row["node_id"] == "node.untouched"
    assert row["judge"] is None
    assert row["finding_signature"] is None
    assert row["bank_code"] is None
    assert row["gate_row"] == "no_judge"
    assert row["gate_decision"] == "drop"


def test_bank_top1_is_global_best_even_when_no_judge_cokey():
    """bank_pattern_top1 reports the single highest-confidence bank finding
    across the whole result — the below-floor best match, regardless of co-key
    (handoff §3, `density_ignored`@0.465)."""
    graph = _graph("node.density")
    # Judge names a DIFFERENT code, so there is no co-key, but the below-floor
    # bank match must still be reported as top-1.
    judge = _judge(concept_key="node.density", confidence=0.95, bank_code=None)
    bank = _bank(bank_code="density_ignored", confidence=0.465, above_floor=False)

    rows = _run((judge, bank), graph)

    assert rows[0]["cokey_bank_code"] is None
    assert rows[0]["bank_pattern_top1"]["bank_code"] == "density_ignored"
    assert rows[0]["bank_pattern_top1"]["similarity"] == 0.465


def test_multiple_nodes_each_get_a_row():
    graph = _graph("n1", "n2", "n3")
    j1 = _judge(concept_key="n1", confidence=0.95, bank_code="a")  # solo dock
    j2 = _judge(concept_key="n2", confidence=0.99, bank_code=None)  # clarify
    bank = _bank(bank_code="a", confidence=0.9, above_floor=True)  # co-keys n1

    rows = _run((j1, j2, bank), graph)

    by_node = {r["node_id"]: r for r in rows}
    assert set(by_node) == {"n1", "n2", "n3"}
    assert by_node["n1"]["gate_row"] == "row3_cokey_dock"
    assert by_node["n2"]["gate_row"] == "row7_unkeyed_clarify"
    assert by_node["n3"]["judge"] is None


# --------------------------------------------------------------------------- #
# false-Strong roll-up
# --------------------------------------------------------------------------- #
def test_is_false_strong_misconception_class_in_strong_band():
    assert is_false_strong(is_control=False, final_band="Strong") is True
    assert (
        is_false_strong(is_control=False, final_band="strong") is True
    )  # case-insensitive


def test_is_false_strong_control_never_flagged():
    assert is_false_strong(is_control=True, final_band="Strong") is False


def test_is_false_strong_misconception_not_strong_band():
    assert is_false_strong(is_control=False, final_band="Developing") is False
    assert is_false_strong(is_control=False, final_band=None) is False


# --------------------------------------------------------------------------- #
# emit_traces + trace_attempt (IO seam)
# --------------------------------------------------------------------------- #
def test_emit_traces_writes_jsonl(tmp_path):
    target = tmp_path / "sub" / "trace.jsonl"  # parent dir does NOT exist yet
    rows = ({"attempt_id": 1, "node_id": "a"}, {"attempt_id": 1, "node_id": "b"})

    emit_traces(rows, path=str(target))

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["node_id"] == "a"
    assert json.loads(lines[1])["node_id"] == "b"


def test_emit_traces_appends(tmp_path):
    target = tmp_path / "trace.jsonl"
    emit_traces(({"node_id": "a"},), path=str(target))
    emit_traces(({"node_id": "b"},), path=str(target))
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["node_id"] for line in lines] == ["a", "b"]


def test_emit_traces_empty_is_noop(tmp_path):
    target = tmp_path / "trace.jsonl"
    emit_traces((), path=str(target))
    assert not target.exists()


def test_emit_traces_soft_fails_on_bad_path(tmp_path):
    """A path whose parent is a FILE (not a dir) cannot be created — emit must
    swallow the error, never raise (a trace defect must never break a grade)."""
    blocker = tmp_path / "iamafile"
    blocker.write_text("x", encoding="utf-8")
    bad = blocker / "nested" / "trace.jsonl"  # parent is a file
    emit_traces(({"node_id": "a"},), path=str(bad))  # must not raise


def test_emit_traces_serializes_non_json_default(tmp_path):
    """default=str keeps the emitter machine-parseable even if a stray
    non-JSON value slips into a row."""
    target = tmp_path / "trace.jsonl"

    class _Weird:
        def __str__(self) -> str:
            return "weird"

    emit_traces(({"node_id": "a", "x": _Weird()},), path=str(target))
    parsed = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert parsed["x"] == "weird"


def test_trace_attempt_emits_and_returns_rows(tmp_path):
    target = tmp_path / "trace.jsonl"
    graph = _graph("node.real_basis")
    judge = _judge(
        concept_key="node.real_basis", confidence=0.95, bank_code="nominal_for_real"
    )
    bank = _bank(bank_code="nominal_for_real", confidence=0.582, above_floor=False)
    findings = (judge, bank)
    gated = gate_findings(findings)

    rows = trace_attempt(
        attempt_id=88,
        reference_graph=graph,
        detection=DetectionResult(per_concept=findings),
        gated=gated,
        outcome=_outcome(penalty=0.27),
        centrality={"node.real_basis": 0.7},
        final_band="Strong",
        is_control=False,
        path=str(target),
    )

    # false-Strong roll-up computed by trace_attempt (misconception-class, band Strong).
    assert rows[0]["is_false_strong"] is True
    assert rows[0]["misconception_penalty"] == 0.27
    # Emitted to disk, machine-parseable.
    on_disk = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert on_disk["node_id"] == "node.real_basis"
    assert on_disk["gate_row"] == "row3_cokey_dock"


def test_trace_attempt_control_not_false_strong(tmp_path):
    target = tmp_path / "trace.jsonl"
    graph = _graph("n")
    judge = _judge(concept_key="n", confidence=0.99, bank_code=None)
    findings = (judge,)
    rows = trace_attempt(
        attempt_id=77,
        reference_graph=graph,
        detection=DetectionResult(per_concept=findings),
        gated=gate_findings(findings),
        outcome=_outcome(),
        centrality={},
        final_band="Strong",
        is_control=True,
        path=str(target),
    )
    assert rows[0]["is_false_strong"] is False


# --------------------------------------------------------------------------- #
# config flag + path seam
# --------------------------------------------------------------------------- #
def test_trace_flag_default_off(monkeypatch):
    monkeypatch.delenv(detector_config.TRACE_FLAG_ENV, raising=False)
    assert detector_config.trace_enabled() is False


def test_trace_flag_truthy(monkeypatch):
    for value in ("1", "true", "yes", "on", "YES"):
        monkeypatch.setenv(detector_config.TRACE_FLAG_ENV, value)
        assert detector_config.trace_enabled() is True


def test_trace_flag_falsy(monkeypatch):
    for value in ("0", "false", "", "garbage"):
        monkeypatch.setenv(detector_config.TRACE_FLAG_ENV, value)
        assert detector_config.trace_enabled() is False


def test_trace_path_default(monkeypatch):
    monkeypatch.delenv(detector_config.TRACE_PATH_ENV, raising=False)
    assert detector_config.trace_path() == detector_config.TRACE_PATH_DEFAULT


def test_trace_path_override(monkeypatch):
    monkeypatch.setenv(detector_config.TRACE_PATH_ENV, "/tmp/custom.jsonl")
    assert detector_config.trace_path() == "/tmp/custom.jsonl"


def test_trace_path_blank_override_falls_back(monkeypatch):
    monkeypatch.setenv(detector_config.TRACE_PATH_ENV, "   ")
    assert detector_config.trace_path() == detector_config.TRACE_PATH_DEFAULT


def test_emit_default_path_used_when_none(monkeypatch, tmp_path):
    """emit_traces(path=None) resolves config.trace_path()."""
    target = tmp_path / "resolved.jsonl"
    monkeypatch.setenv(detector_config.TRACE_PATH_ENV, str(target))
    emit_traces(({"node_id": "a"},))
    assert (
        json.loads(target.read_text(encoding="utf-8").splitlines()[0])["node_id"] == "a"
    )


def test_trace_module_exports():
    assert set(trace_mod.__all__) == {
        "build_node_traces",
        "emit_traces",
        "is_false_strong",
        "trace_attempt",
    }
