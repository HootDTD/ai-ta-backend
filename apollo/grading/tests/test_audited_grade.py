"""WU-4B1 §6.4 — orchestration + handoff-shape tests for build_audited_grade.

Each test injects a deterministic ``audit_fn`` (no live LLM). Carries the four
binding §6.11 behaviour fixtures (parser-miss-then-audit-finds upgrade; high
unresolved abstain; audit-infra failure suppresses all missing; misconception
withhold) plus immutability + score-passthrough pins.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

from apollo.grading.abstention import (
    REASON_HIGH_UNRESOLVED,
    REASON_LOW_NORMALIZATION_CONFIDENCE,
    REASON_TRANSCRIPT_AUDIT_FAILED,
)
from apollo.grading.audited_grade import AUDIT_UPGRADE_MESSAGE, build_audited_grade
from apollo.grading.normalization_confidence import compute_normalization_confidence
from apollo.grading.tests._builders import (
    candidate,
    covered_finding_with_nodes,
    found_audit_fn,
    missing_grade,
    nodes_with_confidences,
    notfound_audit_fn,
    raising_audit_fn,
    resolution_with,
)
from apollo.graph_compare.findings import Finding, FindingKind


def _findings_by_kind(findings, kind):
    return [f for f in findings if f.kind == kind]


# --- §6.11 fixture #1: parser misses a key sentence -> audit FINDS it --------


def test_parser_miss_audit_finds_span_upgrades_no_false_missing():
    grade = missing_grade(("eq.continuity",))
    fn = found_audit_fn({"eq.continuity": "the pipe narrows so the speed rises"})
    out = build_audited_grade(
        grade,
        transcript="...the pipe narrows so the speed rises...",
        resolution=resolution_with(resolved=4),
        student_nodes=(),
        candidates=(candidate("eq.continuity", display_name="Continuity"),),
        audit_fn=fn,
    )
    # NO false missing survives for the audit-found key.
    missing = _findings_by_kind(out.findings, FindingKind.MISSING_NODE)
    assert all(f.canonical_key != "eq.continuity" for f in missing)
    # A covered-grade upgraded finding carries the span + capped confidence + marker.
    covered = _findings_by_kind(out.findings, FindingKind.COVERED_NODE)
    upgraded = [f for f in covered if f.canonical_key == "eq.continuity"]
    assert len(upgraded) == 1
    f = upgraded[0]
    assert f.confidence <= 0.75
    assert "the pipe narrows so the speed rises" in f.evidence_spans
    assert f.message == AUDIT_UPGRADE_MESSAGE
    # exactly one AliasCandidate emitted.
    assert len(out.alias_candidates) == 1
    assert out.alias_candidates[0].canonical_key == "eq.continuity"


def test_audit_found_key_confidence_capped():
    grade = missing_grade(("eq.x",))
    fn = found_audit_fn({"eq.x": "span"})
    out = build_audited_grade(
        grade,
        transcript="span",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        candidates=(candidate("eq.x"),),
        audit_fn=fn,
    )
    upgraded = [
        f for f in out.findings if f.kind == FindingKind.COVERED_NODE and f.canonical_key == "eq.x"
    ]
    assert upgraded[0].confidence == 0.75


def test_audit_not_found_key_stays_missing():
    grade = missing_grade(("eq.x",))
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        candidates=(candidate("eq.x"),),
        audit_fn=notfound_audit_fn(),
    )
    missing = _findings_by_kind(out.findings, FindingKind.MISSING_NODE)
    assert any(f.canonical_key == "eq.x" for f in missing)
    assert out.alias_candidates == ()


# --- §6.11 fixture #3: audit-infra failure suppresses ALL missing -----------


def test_audit_infra_failure_suppresses_all_missing_and_records_reason():
    grade = missing_grade(("eq.a", "eq.b"))
    out = build_audited_grade(  # MUST NOT raise — error caught at the boundary.
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        candidates=(candidate("eq.a"), candidate("eq.b")),
        audit_fn=raising_audit_fn(),
    )
    # NO upgrade (both stay missing as findings) ...
    missing = _findings_by_kind(out.findings, FindingKind.MISSING_NODE)
    assert {f.canonical_key for f in missing} == {"eq.a", "eq.b"}
    assert _findings_by_kind(out.findings, FindingKind.COVERED_NODE) == []
    # ... but the missing EVENT kind is suppressed and the reason is RECORDED
    # (proof the named error was surfaced, not silently swallowed).
    assert "missing" in out.suppressed_event_kinds
    assert REASON_TRANSCRIPT_AUDIT_FAILED in out.abstention_reasons
    assert out.alias_candidates == ()


# --- §6.11 fixture #2: high unresolved -> abstain, findings preserved -------


def test_high_unresolved_run_abstains_findings_preserved():
    grade = missing_grade(("eq.x",))
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(unresolved=3, resolved=2),  # rate 0.6 > 0.35
        student_nodes=(),
        candidates=(candidate("eq.x"),),
        audit_fn=notfound_audit_fn(),
    )
    assert out.abstained is True
    assert REASON_HIGH_UNRESOLVED in out.abstention_reasons
    # findings still fully populated for the diagnostic + WU-4B3 persistence.
    assert len(out.findings) == len(grade.findings)


# --- §6.11 fixture #5: misconception withhold (in the grade) ----------------


def test_misconception_low_confidence_withheld_in_grade():
    # Real-shaped contradiction: Finding.confidence is None (as graph_compare's
    # factory emits); the §6.6 gate MUST source confidence from the RESOLUTION.
    grade = missing_grade((), contradictions=(("misc.density_ignored", ("s1",)),))
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2, resolved_nodes=(("s1", 0.7),)),
        student_nodes=(),
        audit_fn=notfound_audit_fn(),
    )
    assert "misconception" in out.suppressed_event_kinds
    # the contradiction finding still persists for diagnostic review.
    contradictions = _findings_by_kind(out.findings, FindingKind.CONTRADICTION)
    assert any(f.canonical_key == "misc.density_ignored" for f in contradictions)


def test_misconception_high_confidence_not_withheld():
    grade = missing_grade((), contradictions=(("misc.x", ("s1",)),))
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2, resolved_nodes=(("s1", 0.9),)),
        student_nodes=(),
        audit_fn=notfound_audit_fn(),
    )
    assert "misconception" not in out.suppressed_event_kinds


def test_misconception_gate_reads_resolution_not_finding_confidence():
    """Regression for the review-FAIL defect: the contradiction Finding carries
    confidence=None (real factory), so the §6.6 gate MUST read the misconception
    confidence off the resolution. If it read Finding.confidence (None -> 1.0) the
    gate would be permanently inert and never withhold here."""
    grade = missing_grade((), contradictions=(("misc.y", ("s1",)),))
    assert grade.findings[0].confidence is None  # real-shaped: no injected confidence
    out = build_audited_grade(
        grade,
        transcript="t",
        # ONLY the resolution carries the (low) confidence — 0.5 < 0.8.
        resolution=resolution_with(resolved=2, resolved_nodes=(("s1", 0.5),)),
        student_nodes=(),
        audit_fn=notfound_audit_fn(),
    )
    assert "misconception" in out.suppressed_event_kinds


def test_misconception_confidence_is_max_over_evidence_and_skips_unresolved():
    """Per-finding confidence = MAX over its resolved evidence nodes (mirrors
    S_norm's highest-cap-wins merge), and a contradiction whose evidence is
    unresolved contributes nothing. misc.a: max(0.6, 0.95)=0.95 (>=0.8 -> NOT
    withheld; min would be 0.6 and would wrongly withhold). misc.b: its evidence
    node is absent from the resolution -> contributes nothing."""
    grade = missing_grade(
        (),
        contradictions=(("misc.a", ("a1", "a2")), ("misc.b", ("ghost",))),
    )
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2, resolved_nodes=(("a1", 0.6), ("a2", 0.95))),
        student_nodes=(),
        audit_fn=notfound_audit_fn(),
    )
    assert "misconception" not in out.suppressed_event_kinds


# --- immutability + passthrough ---------------------------------------------


def test_build_audited_grade_is_immutable():
    grade = missing_grade(("eq.x",))
    original_findings = grade.findings
    original_first = original_findings[0]
    fn = found_audit_fn({"eq.x": "span"})
    out = build_audited_grade(
        grade,
        transcript="span",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        candidates=(candidate("eq.x"),),
        audit_fn=fn,
    )
    # input GradeResult + its findings untouched (identity-compared).
    assert grade.findings is original_findings
    assert grade.findings[0] is original_first
    # rewritten findings are a NEW tuple.
    assert out.findings is not original_findings


def test_build_audited_grade_no_missing_nodes_noop():
    grade = missing_grade((), covered=("eq.covered",))
    fn = found_audit_fn({})
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        audit_fn=fn,
    )
    assert fn.requests == []  # type: ignore[attr-defined]  # no entities => no call
    assert out.abstention_reasons == ()
    assert out.findings == grade.findings


def test_audited_grade_carries_score_math_unchanged():
    grade = missing_grade(("eq.x",))
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        candidates=(candidate("eq.x"),),
        audit_fn=notfound_audit_fn(),
    )
    assert out.grade is grade
    assert out.grade.coverage_score == grade.coverage_score
    assert out.grade.bisimilarity_score == grade.bisimilarity_score


def test_default_audit_fn_is_main_chat_auditor():
    grade = missing_grade(("eq.x",))
    payload = json.dumps({"spans": {"eq.x": None}})  # found nothing, but DROVE the wrapper
    with patch("apollo.grading.transcript_audit.main_chat", return_value=payload) as mock_chat:
        out = build_audited_grade(
            grade,
            transcript="short",
            resolution=resolution_with(resolved=2),
            student_nodes=(),
            candidates=(candidate("eq.x"),),
            audit_fn=None,  # default => live wrapper, no live API (patched)
        )
    assert mock_chat.call_count == 1
    # nothing found => stays missing, no alias.
    assert out.alias_candidates == ()


def test_missing_entity_display_name_falls_back_to_key():
    # eq.x has a missing_node finding but is ABSENT from candidates.
    grade = missing_grade(("eq.x",))
    captured = {}

    def fn(request):
        captured["entities"] = request.entities
        return {e.canonical_key: None for e in request.entities}

    build_audited_grade(
        grade,
        transcript="t",
        resolution=resolution_with(resolved=2),
        student_nodes=(),
        candidates=(),  # no candidates => fallback
        audit_fn=fn,
    )
    assert captured["entities"][0].display_name == "eq.x"
    assert captured["entities"][0].canonical_key == "eq.x"


def test_build_audited_grade_forwards_misconception_bank_empty():
    from apollo.grading.abstention import REASON_MISCONCEPTION_BANK_EMPTY
    grade = missing_grade()
    out = build_audited_grade(
        grade,
        transcript="hello",
        resolution=resolution_with(),
        student_nodes=nodes_with_confidences(1.0),
        misconception_bank_empty=True,
        audit_fn=notfound_audit_fn(),
    )
    assert REASON_MISCONCEPTION_BANK_EMPTY in out.abstention_reasons
    assert out.abstained is False


# --- Phase 1c: normalization-confidence brake + D1 reorder -------------------


def test_reorder_preserves_persisted_nc_value():
    """The nc the gate sees internally == the value an EXTERNAL
    compute_normalization_confidence(out, resolution) yields (the persisted nc at
    done_grading.py:265). With nothing scored-with-backer the neutral floor (1.0)
    holds and the 1c gate does NOT fire."""
    grade = missing_grade(("eq.x",))
    res = resolution_with(resolved=2)
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=res,
        student_nodes=(),
        candidates=(candidate("eq.x"),),
        audit_fn=notfound_audit_fn(),
    )
    # external recompute over the SAME audited grade == what the gate saw internally
    assert compute_normalization_confidence(out, res) == 1.0  # neutral floor
    assert out.abstained is False
    assert REASON_LOW_NORMALIZATION_CONFIDENCE not in out.abstention_reasons


def test_weak_tier_backing_triggers_1c_abstain_end_to_end():
    """A covered finding backed by a single weak (fuzzy-cap 0.80) resolved node ->
    internal nc 0.80 < 0.85 -> abstained=True with the 1c reason, even though
    unresolved_rate is 0 (the SECOND, quality-keyed abstain trigger)."""
    grade = missing_grade()
    grade = replace(grade, findings=(covered_finding_with_nodes("k.a", ("a1",)),))
    res = resolution_with(resolved_nodes=(("a1", 0.80),))  # one weak (fuzzy-cap) backer
    out = build_audited_grade(
        grade,
        transcript="t",
        resolution=res,
        student_nodes=(),
        candidates=(candidate("k.a"),),
        audit_fn=notfound_audit_fn(),
    )
    assert out.abstained is True
    assert REASON_LOW_NORMALIZATION_CONFIDENCE in out.abstention_reasons
    # the persisted-value invariant: the internal gate nc == the external recompute.
    assert compute_normalization_confidence(out, res) == 0.80


def test_nc_is_computed_over_post_rewrite_findings_discriminator():
    """REQUIRED [HIGH]-risk discriminator (D1). An audit upgrade turns a
    MISSING_NODE into a COVERED_NODE (a SCORED kind). This test proves the gate's
    nc is computed over the POST-rewrite findings: it spies on the helper actually
    used by build_audited_grade and asserts the findings tuple it received is the
    rewritten one (carrying the upgraded COVERED_NODE for eq.x), NOT grade.findings
    (which still carries the MISSING_NODE). A pre-rewrite implementation would pass
    grade.findings here and the assertion would fail.

    NOTE: with the stock _upgraded_finding (which carries no student_node_ids) the
    upgraded COVERED_NODE has no resolved backer, so nc's *value* is the same over
    pre- and post-rewrite findings; the load-bearing distinction the gate depends
    on is therefore asserted directly on the findings argument, not via a value
    divergence."""
    from apollo.grading.normalization_confidence import _normalization_confidence_over

    grade = missing_grade(("eq.x",))
    fn = found_audit_fn({"eq.x": "the student explained eq.x here"})
    res = resolution_with(resolved=2)

    seen: dict[str, tuple[Finding, ...]] = {}

    def _spy(findings, resolution):
        seen["findings"] = findings
        return _normalization_confidence_over(findings, resolution)

    with patch(
        "apollo.grading.audited_grade._normalization_confidence_over", side_effect=_spy
    ):
        out = build_audited_grade(
            grade,
            transcript="t",
            resolution=res,
            student_nodes=(),
            candidates=(candidate("eq.x"),),
            audit_fn=fn,
        )

    # the helper was fed the POST-rewrite findings: eq.x is now a COVERED_NODE,
    # and NO MISSING_NODE for eq.x survives in the argument.
    fed = seen["findings"]
    assert fed is out.findings  # same tuple identity as the constructed (rewritten) findings
    assert any(f.kind == FindingKind.COVERED_NODE and f.canonical_key == "eq.x" for f in fed)
    assert not any(f.kind == FindingKind.MISSING_NODE and f.canonical_key == "eq.x" for f in fed)
    # sanity: had it been pre-rewrite findings, eq.x would still be a MISSING_NODE.
    assert any(
        f.kind == FindingKind.MISSING_NODE and f.canonical_key == "eq.x" for f in grade.findings
    )
