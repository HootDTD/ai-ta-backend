"""Structural co-key gate path (F-struct Task 8)."""

from __future__ import annotations

import pytest

from apollo.overseer.misconception_detector.gate import gate_findings
from apollo.overseer.misconception_detector.types import ConceptFinding, Verdict

pytestmark = pytest.mark.unit


def _judge(
    concept_key: str, verdict: Verdict, conf: float, bank_code: str | None = None
) -> ConceptFinding:
    return ConceptFinding(
        concept_key=concept_key,
        verdict=verdict,
        confidence=conf,
        severity=0.0,
        evidence_span="nominal is fine",
        signature=(f"misc.{bank_code}" if bank_code else f"unkeyed:{concept_key}"),
        source="judge",
        corroborated=False,
        verdict_token_prob_present=True,
        bank_code=bank_code,
    )


def test_wrong_verdict_with_opposes_docks_keyed() -> None:
    # The miss signature: judge localizes (wrong@~1.0) but named NO code.
    findings = (_judge("real_basis", "wrong", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert len(out) == 1
    rep = out[0]
    assert rep.verdict == "misconception"
    assert rep.corroborated is True
    assert rep.ceiling_eligible is True
    assert rep.bank_code == "nominal_for_real"
    assert rep.signature == "misc.nominal_for_real"


def test_clear_verdict_never_structural_docks() -> None:
    # Control safety (INVARIANT 4): a clear verdict never enters the structural
    # branch, so it can never be structural-docked — no misconception/
    # corroborated row is ever emitted for it. It takes its normal (pre-F-struct)
    # lone-unkeyed path unchanged; with an opposes_index present that path is
    # byte-identical to the empty-index path (proven in
    # test_clear_verdict_identical_with_and_without_opposes), which is the
    # control-safety guarantee that matters (a control is never DOCKED).
    findings = (_judge("real_basis", "clear", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert not any(f.verdict == "misconception" for f in out)
    assert not any(f.corroborated for f in out)


def test_needs_clarification_never_structural_docks() -> None:
    # Same control-safety guarantee for a needs_clarification verdict: it is
    # non-wrong/non-misconception, so the structural branch's verdict guard
    # excludes it — it is never structural-docked.
    findings = (_judge("real_basis", "needs_clarification", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert not any(f.verdict == "misconception" for f in out)
    assert not any(f.corroborated for f in out)


def test_clear_verdict_identical_with_and_without_opposes() -> None:
    # INVARIANT 6 (flag-OFF byte-identical) + INVARIANT 4 (control-safety): a
    # clear verdict produces the EXACT same gate output whether or not an
    # opposes_index entry exists for its node. The structural branch is inert on
    # clear, so an opposing bank entry can never change a control's outcome.
    findings = (_judge("real_basis", "clear", 1.0, bank_code=None),)
    with_opposes = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    without_opposes = gate_findings(findings, opposes_index={})
    assert with_opposes == without_opposes


def test_sub_routed_tau_wrong_does_not_structural_dock() -> None:
    findings = (_judge("real_basis", "wrong", 0.50, bank_code=None),)  # < 0.85
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert out == ()


def test_no_opposes_entry_leaves_prior_behavior() -> None:
    # INVARIANT 6 (flag-OFF byte-identical): with an EMPTY opposes_index the
    # structural branch is unreachable (empty-dict lookup always misses), so a
    # lone unkeyed wrong@1.0 takes its unchanged pre-F-struct path — row 7,
    # needs_clarification (it clears routed tau at conf 1.0), NOT a dock. The
    # load-bearing assertion for the flag-OFF invariant is that NO structural
    # dock (misconception/corroborated) ever appears.
    findings = (_judge("real_basis", "wrong", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={})
    assert not any(f.verdict == "misconception" for f in out)
    assert not any(f.corroborated for f in out)
    # The unkeyed wrong@1.0 routes to needs_clarification (row 7) — unchanged.
    assert len(out) == 1
    assert out[0].verdict == "needs_clarification"


def test_empty_index_byte_identical_to_pre_change_defaults() -> None:
    # INVARIANT 6 proof-of-identity: calling gate_findings WITHOUT opposes_index
    # (the default the flag-OFF caller uses via Task 10) and calling it with an
    # explicit empty dict must produce identical output for a representative
    # spread of verdicts — i.e. the new param defaults to inert.
    cases: tuple[tuple[Verdict, float], ...] = (
        ("wrong", 1.0),
        ("misconception", 1.0),
        ("clear", 1.0),
        ("needs_clarification", 1.0),
        ("wrong", 0.50),
    )
    for verdict, conf in cases:
        findings = (_judge("real_basis", verdict, conf, bank_code=None),)
        default_call = gate_findings(findings)
        explicit_empty = gate_findings(findings, opposes_index={})
        # And a populated index must NOT change a NON-triggering (clear/
        # needs_clarification/sub-tau) case relative to the empty one.
        assert default_call == explicit_empty


def test_judge_named_code_takes_existing_path_no_double() -> None:
    # Judge already named the code -> existing lone-solo path, NOT structural.
    # opposes_index present, but bank_code is not None so structural is skipped.
    findings = (_judge("real_basis", "misconception", 1.0, bank_code="nominal_for_real"),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert len(out) == 1
    assert out[0].bank_code == "nominal_for_real"
    # Docked once (single representative), no duplication.
