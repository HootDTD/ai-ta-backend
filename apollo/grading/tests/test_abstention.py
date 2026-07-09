"""WU-4B1 §6.3 — pure-unit tests for the §6.6 abstention gates.

One test per gate row + the helpers. Includes the binding MIN-not-mean parser
confidence proof and the abstained-vs-partial-suppression distinction.
"""

from __future__ import annotations

from apollo.grading.abstention import (
    ABSTENTION_THRESHOLDS,
    REASON_COMPOSITE_LOW_COVERAGE,
    REASON_HIGH_UNRESOLVED,
    REASON_LOW_MISCONCEPTION_CONFIDENCE,
    REASON_LOW_NORMALIZATION_CONFIDENCE,
    REASON_LOW_PARSER_CONFIDENCE,
    REASON_REFERENCE_INVALID,
    REASON_TRANSCRIPT_AUDIT_FAILED,
    apply_abstention,
    min_parser_confidence_of,
    unresolved_rate_of,
)
from apollo.grading.tests._builders import nodes_with_confidences, resolution_with


def _clean(**overrides):
    base = dict(unresolved_rate=0.0, min_parser_confidence=1.0)
    base.update(overrides)
    return apply_abstention(**base)


# --- unresolved-rate gate (the ONLY gate that sets abstained) ---------------


def test_high_unresolved_rate_abstains():
    out = _clean(unresolved_rate=0.5)
    assert out.abstained is True
    assert REASON_HIGH_UNRESOLVED in out.abstention_reasons


def test_unresolved_rate_at_threshold_does_not_abstain():
    out = _clean(unresolved_rate=0.35)  # 0.35 is NOT > 0.35
    assert out.abstained is False
    assert REASON_HIGH_UNRESOLVED not in out.abstention_reasons


# --- normalization-confidence gate (Phase 1c: the SECOND gate that abstains) ----


def test_threshold_constant_importable_and_is_085():
    assert REASON_LOW_NORMALIZATION_CONFIDENCE  # importable, non-empty
    assert ABSTENTION_THRESHOLDS["min_normalization_confidence"] == 0.85


def test_low_normalization_confidence_abstains():
    # below 0.85, unresolved_rate stays 0.0 -> the nc gate alone abstains.
    out = _clean(normalization_confidence=0.80)
    assert out.abstained is True
    assert REASON_LOW_NORMALIZATION_CONFIDENCE in out.abstention_reasons


def test_normalization_confidence_at_threshold_does_not_abstain():
    out = _clean(normalization_confidence=0.85)  # 0.85 is NOT < 0.85
    assert REASON_LOW_NORMALIZATION_CONFIDENCE not in out.abstention_reasons
    assert out.abstained is False


def test_default_normalization_confidence_does_not_abstain():
    out = _clean()  # default 1.0 never trips -> backward compatible
    assert REASON_LOW_NORMALIZATION_CONFIDENCE not in out.abstention_reasons
    assert out.abstained is False


# --- min parser-confidence gate (suppress 'missing', NOT abstain) -----------


def test_low_min_parser_confidence_suppresses_missing():
    out = _clean(min_parser_confidence=0.5)
    assert "missing" in out.suppressed_event_kinds
    assert REASON_LOW_PARSER_CONFIDENCE in out.abstention_reasons
    assert out.abstained is False  # partial suppression, not full abstain


def test_parser_confidence_min_not_mean():
    """MIN 0.40 < 0.6 trips the gate; the MEAN 0.8125 >= 0.6 would NOT."""
    nodes = nodes_with_confidences(0.95, 0.95, 0.95, 0.40)
    assert min_parser_confidence_of(nodes) == 0.40
    mean = sum(n.parser_confidence for n in nodes) / len(nodes)
    assert mean >= 0.6  # mean would NOT trip the gate
    out = _clean(min_parser_confidence=min_parser_confidence_of(nodes))
    assert "missing" in out.suppressed_event_kinds
    assert REASON_LOW_PARSER_CONFIDENCE in out.abstention_reasons


def test_parser_confidence_at_threshold_does_not_suppress():
    out = _clean(min_parser_confidence=0.6)  # 0.6 is NOT < 0.6
    assert "missing" not in out.suppressed_event_kinds


# --- transcript-audit-failure gate (suppress all 'missing') -----------------


def test_audit_failure_suppresses_all_missing():
    out = _clean(transcript_audit_failed=True)
    assert "missing" in out.suppressed_event_kinds
    assert REASON_TRANSCRIPT_AUDIT_FAILED in out.abstention_reasons
    assert out.abstained is False


# --- misconception-confidence gate (withhold 'misconception') ---------------


def test_low_misconception_confidence_withholds():
    out = _clean(misconception_confidences=(0.7,))
    assert "misconception" in out.suppressed_event_kinds
    assert REASON_LOW_MISCONCEPTION_CONFIDENCE in out.abstention_reasons


def test_high_misconception_confidence_does_not_withhold():
    out = _clean(misconception_confidences=(0.85,))
    assert "misconception" not in out.suppressed_event_kinds
    assert REASON_LOW_MISCONCEPTION_CONFIDENCE not in out.abstention_reasons


def test_misconception_confidence_at_threshold_does_not_withhold():
    out = _clean(misconception_confidences=(0.8,))  # 0.8 is NOT < 0.8
    assert "misconception" not in out.suppressed_event_kinds


def test_misconception_any_low_one_withholds():
    out = _clean(misconception_confidences=(0.9, 0.7, 0.95))
    assert "misconception" in out.suppressed_event_kinds


# --- reference-invalid gate (record only) -----------------------------------


def test_reference_invalid_recorded_not_raised():
    out = _clean(reference_invalid=True)
    assert REASON_REFERENCE_INVALID in out.abstention_reasons


# --- clean / determinism ----------------------------------------------------


def test_no_gates_clean_run():
    out = _clean()
    assert out.abstention_reasons == ()
    assert out.abstained is False
    assert out.suppressed_event_kinds == frozenset()


def test_reasons_deterministic_order():
    kwargs = dict(
        unresolved_rate=0.9,
        min_parser_confidence=0.1,
        misconception_confidences=(0.1,),
        transcript_audit_failed=True,
        reference_invalid=True,
        normalization_confidence=0.0,
    )
    first = apply_abstention(**kwargs).abstention_reasons
    second = apply_abstention(**kwargs).abstention_reasons
    assert first == second
    # gate-declaration order: unresolved -> normalization -> parser -> audit ->
    # misconception -> ref. (the nc gate sits right after unresolved — both abstain.)
    assert first == (
        REASON_HIGH_UNRESOLVED,
        REASON_LOW_NORMALIZATION_CONFIDENCE,
        REASON_LOW_PARSER_CONFIDENCE,
        REASON_TRANSCRIPT_AUDIT_FAILED,
        REASON_LOW_MISCONCEPTION_CONFIDENCE,
        REASON_REFERENCE_INVALID,
    )


# --- composite abstention gate (§10, APOLLO_ABSTENTION_COMPOSITE) -----------
# Flag-OFF (default) is byte-identical to every test above (composite_enabled
# defaults False, ``composite`` metadata defaults None). These pin the flag-ON
# behavior: coverage >= threshold grades REGARDLESS of unresolved_rate/nc;
# coverage < threshold abstains with a NEW dedicated reason; contradictions are
# recorded but never force abstention on their own.


def test_composite_disabled_by_default_is_byte_identical():
    """Composite metadata is None and the old unresolved_rate gate still fully
    governs ``abstained`` when the flag is not passed at all."""
    out = _clean(unresolved_rate=0.9)
    assert out.abstained is True
    assert out.composite is None
    assert REASON_COMPOSITE_LOW_COVERAGE not in out.abstention_reasons


def test_composite_high_coverage_grades_despite_high_unresolved_rate():
    """The whole point of §10: a high unresolved_rate (volume signal) must NOT
    abstain a high-coverage (content signal) attempt when the flag is ON."""
    out = _clean(unresolved_rate=0.9, composite_enabled=True, node_coverage=0.8)
    assert out.abstained is False
    assert REASON_COMPOSITE_LOW_COVERAGE not in out.abstention_reasons
    assert out.composite == {
        "coverage": 0.8,
        "contradictions": 0,
        "coverage_min": 0.6,
        "decision": "grade",
    }


def test_composite_low_coverage_abstains_with_dedicated_reason():
    out = _clean(unresolved_rate=0.0, composite_enabled=True, node_coverage=0.4)
    assert out.abstained is True
    assert REASON_COMPOSITE_LOW_COVERAGE in out.abstention_reasons
    assert out.composite["decision"] == "abstain"


def test_composite_coverage_at_threshold_grades():
    """0.6 is NOT < 0.6 (>= threshold grades)."""
    out = _clean(composite_enabled=True, node_coverage=0.6)
    assert out.abstained is False
    assert out.composite["decision"] == "grade"


def test_composite_custom_threshold_respected():
    out = _clean(composite_enabled=True, node_coverage=0.75, coverage_min=0.8)
    assert out.abstained is True
    assert out.composite["coverage_min"] == 0.8


def test_composite_contradictions_recorded_but_do_not_force_abstain():
    """A misconception detection (contradiction findings > 0) is informative,
    not an abstention trigger, when coverage clears the bar."""
    out = _clean(composite_enabled=True, node_coverage=0.9, contradiction_count=2)
    assert out.abstained is False
    assert out.composite["contradictions"] == 2


def test_composite_preserves_existing_reasons_as_metadata():
    """Old volume-gate reasons still get RECORDED (informative/auditable) even
    though they no longer drive ``abstained`` when composite is enabled."""
    out = _clean(
        unresolved_rate=0.9,
        normalization_confidence=0.1,
        composite_enabled=True,
        node_coverage=0.9,
    )
    assert out.abstained is False  # composite overrides both volume gates
    assert REASON_HIGH_UNRESOLVED in out.abstention_reasons
    assert REASON_LOW_NORMALIZATION_CONFIDENCE in out.abstention_reasons


def test_composite_reason_constant_value():
    assert REASON_COMPOSITE_LOW_COVERAGE == "composite_low_coverage"


# --- helpers ----------------------------------------------------------------


def test_unresolved_rate_of_helper():
    res = resolution_with(unresolved=1, resolved=3)
    assert unresolved_rate_of(res) == 0.25
    empty = resolution_with()
    assert unresolved_rate_of(empty) == 0.0


def test_min_parser_confidence_of_empty_is_one():
    assert min_parser_confidence_of(()) == 1.0


def test_min_parser_confidence_of_picks_min():
    nodes = nodes_with_confidences(0.8, 0.3, 0.9)
    assert min_parser_confidence_of(nodes) == 0.3


# --- Lane B3a/D1: empty misconception bank is NORMAL, not an abstention ------
# Under the emergent-misconception design an empty ``apollo_misconceptions`` bank
# is the cold-start state of EVERY class. Coverage grading and misconception
# detection are separable signals; an empty bank must NOT gate coverage grading
# and must NOT emit an abstention reason (the reason string polluted harness
# histograms). The bank-empty fact now rides on ``GradeResult.soundness_applicable``
# and surfaces as an explicit artifact marker, NOT as an abstention reason.


def test_apply_abstention_has_no_misconception_bank_empty_param():
    """``apply_abstention`` no longer accepts a ``misconception_bank_empty`` kwarg —
    the empty-bank signal was moved OUT of the abstention gate entirely."""
    import inspect

    assert "misconception_bank_empty" not in inspect.signature(apply_abstention).parameters


def test_no_misconception_bank_empty_reason_constant():
    """The ``misconception_bank_empty`` abstention reason string is REMOVED
    (interface change for harness histograms — it never occurs anywhere now)."""
    import apollo.grading.abstention as abstention_module

    assert not hasattr(abstention_module, "REASON_MISCONCEPTION_BANK_EMPTY")


def test_clean_attempt_has_no_bank_empty_reason():
    """A clean attempt (all gates pass) abstains from nothing and emits zero
    reasons — proving an empty bank can never produce a bank-empty reason."""
    out = apply_abstention(unresolved_rate=0.0, min_parser_confidence=1.0)
    assert out.abstention_reasons == ()
    assert out.abstained is False
    assert "misconception_bank_empty" not in out.abstention_reasons
