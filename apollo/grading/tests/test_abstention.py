"""WU-4B1 §6.3 — pure-unit tests for the §6.6 abstention gates.

One test per gate row + the helpers. Includes the binding MIN-not-mean parser
confidence proof and the abstained-vs-partial-suppression distinction.
"""

from __future__ import annotations

from apollo.grading.abstention import (
    REASON_HIGH_UNRESOLVED,
    REASON_LOW_MISCONCEPTION_CONFIDENCE,
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
    )
    first = apply_abstention(**kwargs).abstention_reasons
    second = apply_abstention(**kwargs).abstention_reasons
    assert first == second
    # gate-declaration order: unresolved -> parser -> audit -> misconception -> ref.
    assert first == (
        REASON_HIGH_UNRESOLVED,
        REASON_LOW_PARSER_CONFIDENCE,
        REASON_TRANSCRIPT_AUDIT_FAILED,
        REASON_LOW_MISCONCEPTION_CONFIDENCE,
        REASON_REFERENCE_INVALID,
    )


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
