"""WU-4B1 §6.3 — pure-unit tests for the §6.6 abstention gates.

One test per gate row + the helpers. Includes the binding MIN-not-mean parser
confidence proof and the abstained-vs-partial-suppression distinction.
"""

from __future__ import annotations

from apollo.grading.abstention import (
    _ABSTENTION_DENOM_V2_FLAG,
    ABSTENTION_THRESHOLDS,
    REASON_HIGH_UNRESOLVED,
    REASON_LOW_MISCONCEPTION_CONFIDENCE,
    REASON_LOW_NORMALIZATION_CONFIDENCE,
    REASON_LOW_PARSER_CONFIDENCE,
    REASON_REFERENCE_INVALID,
    REASON_TRANSCRIPT_AUDIT_FAILED,
    apply_abstention,
    min_parser_confidence_of,
    unresolved_rate_for_abstention,
    unresolved_rate_of,
    unresolved_rate_of_v2,
)
from apollo.grading.tests._builders import nodes_with_confidences, resolution_with
from apollo.resolution.result import ResolutionResult, ResolvedNode


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


def test_misconception_bank_empty_reason_is_reason_only():
    from apollo.grading.abstention import REASON_MISCONCEPTION_BANK_EMPTY

    out = apply_abstention(
        unresolved_rate=0.0, min_parser_confidence=1.0, misconception_bank_empty=True
    )
    assert REASON_MISCONCEPTION_BANK_EMPTY in out.abstention_reasons
    assert out.abstained is False  # coverage still updates Layer-3


def test_misconception_bank_empty_false_no_reason():
    from apollo.grading.abstention import REASON_MISCONCEPTION_BANK_EMPTY

    out = apply_abstention(
        unresolved_rate=0.0, min_parser_confidence=1.0, misconception_bank_empty=False
    )
    assert REASON_MISCONCEPTION_BANK_EMPTY not in out.abstention_reasons


# --- A1 iter1 (G1): unresolved_rate_of_v2 — structural denominator ----------
#
# Dormant behind APOLLO_ABSTENTION_DENOM_V2 (default OFF). Guiding principle:
# abstention should fire when the grader COULDN'T FOLLOW what the student
# taught, not when the parser was chatty. v2 restricts the unresolved_rate
# denominator to student nodes whose node_type has a structural counterpart
# in THIS problem's candidate set (apollo/resolution/candidates.py) — a
# student node of a type absent from the candidate set can never resolve
# (resolver.py::type_compatible hard-gates on node_type), so it is parser
# noise, not a grading failure.


def _rn(node_id: str, resolution: str) -> ResolvedNode:
    return ResolvedNode(
        node_id=node_id,
        resolution=resolution,
        resolved_key=None if resolution != "resolved" else "k",
        resolved_canon_key=None,
        method="exact" if resolution == "resolved" else "unresolved",
        confidence=1.0 if resolution == "resolved" else 0.0,
    )


def test_unresolved_rate_of_v2_excludes_structurally_unresolvable_types():
    # 1 resolved equation + 1 unresolved equation (relevant) + 2 unresolved
    # definitions (irrelevant: candidate set has no `definition` candidate).
    resolution = ResolutionResult(
        resolved=(
            _rn("r0", "resolved"),
            _rn("u0", "unresolved"),
            _rn("u1", "unresolved"),
            _rn("u2", "unresolved"),
        ),
        tier_counts={},
        llm_calls=0,
    )
    node_type_by_id = {
        "r0": "equation",
        "u0": "equation",
        "u1": "definition",
        "u2": "definition",
    }
    candidate_types = frozenset({"equation"})
    # v1 counts everything: 3 unresolved / 4 total.
    assert unresolved_rate_of(resolution) == 0.75
    # v2 excludes the two `definition` nodes (no reference peer): 1/2.
    assert unresolved_rate_of_v2(resolution, node_type_by_id, candidate_types) == 0.5


def test_unresolved_rate_of_v2_equals_v1_when_all_types_relevant():
    resolution = ResolutionResult(
        resolved=(_rn("r0", "resolved"), _rn("u0", "unresolved"), _rn("u1", "unresolved")),
        tier_counts={},
        llm_calls=0,
    )
    node_type_by_id = {"r0": "equation", "u0": "equation", "u1": "condition"}
    candidate_types = frozenset({"equation", "condition"})
    assert unresolved_rate_of_v2(resolution, node_type_by_id, candidate_types) == (
        unresolved_rate_of(resolution)
    )


def test_unresolved_rate_of_v2_empty_relevant_set_is_zero():
    resolution = ResolutionResult(resolved=(_rn("u0", "unresolved"),), tier_counts={}, llm_calls=0)
    node_type_by_id = {"u0": "definition"}
    candidate_types = frozenset({"equation"})
    assert unresolved_rate_of_v2(resolution, node_type_by_id, candidate_types) == 0.0


def test_unresolved_rate_of_v2_missing_type_entry_treated_as_irrelevant():
    resolution = ResolutionResult(
        resolved=(_rn("r0", "resolved"), _rn("u0", "unresolved")), tier_counts={}, llm_calls=0
    )
    # "u0" absent from the map -> defensively excluded, not assumed countable.
    node_type_by_id = {"r0": "equation"}
    candidate_types = frozenset({"equation"})
    assert unresolved_rate_of_v2(resolution, node_type_by_id, candidate_types) == 0.0


def test_unresolved_rate_for_abstention_flag_off_is_byte_identical_to_v1(monkeypatch):
    monkeypatch.delenv(_ABSTENTION_DENOM_V2_FLAG, raising=False)
    resolution = ResolutionResult(
        resolved=(
            _rn("r0", "resolved"),
            _rn("u0", "unresolved"),
            _rn("u1", "unresolved"),
            _rn("u2", "unresolved"),
        ),
        tier_counts={},
        llm_calls=0,
    )
    node_type_by_id = {"r0": "equation", "u0": "equation", "u1": "definition", "u2": "definition"}
    candidate_types = frozenset({"equation"})
    # v2 WOULD differ (0.5 vs 0.75) if the flag were honored -> proves the
    # selector really ignores the v2 inputs when the flag is off.
    assert unresolved_rate_of_v2(resolution, node_type_by_id, candidate_types) == 0.5
    got = unresolved_rate_for_abstention(
        resolution, node_type_by_id=node_type_by_id, candidate_types=candidate_types
    )
    assert got == unresolved_rate_of(resolution) == 0.75


def test_unresolved_rate_for_abstention_flag_off_ignores_missing_v2_inputs(monkeypatch):
    monkeypatch.delenv(_ABSTENTION_DENOM_V2_FLAG, raising=False)
    resolution = resolution_with(unresolved=1, resolved=3)
    assert unresolved_rate_for_abstention(resolution) == unresolved_rate_of(resolution) == 0.25


def test_unresolved_rate_for_abstention_flag_on_uses_v2(monkeypatch):
    monkeypatch.setenv(_ABSTENTION_DENOM_V2_FLAG, "1")
    resolution = ResolutionResult(
        resolved=(
            _rn("r0", "resolved"),
            _rn("u0", "unresolved"),
            _rn("u1", "unresolved"),
            _rn("u2", "unresolved"),
        ),
        tier_counts={},
        llm_calls=0,
    )
    node_type_by_id = {"r0": "equation", "u0": "equation", "u1": "definition", "u2": "definition"}
    candidate_types = frozenset({"equation"})
    got = unresolved_rate_for_abstention(
        resolution, node_type_by_id=node_type_by_id, candidate_types=candidate_types
    )
    assert got == 0.5 != unresolved_rate_of(resolution)


def test_unresolved_rate_for_abstention_flag_on_without_v2_inputs_falls_back_to_v1(monkeypatch):
    monkeypatch.setenv(_ABSTENTION_DENOM_V2_FLAG, "1")
    resolution = resolution_with(unresolved=1, resolved=3)
    assert unresolved_rate_for_abstention(resolution) == unresolved_rate_of(resolution) == 0.25


def test_attempt18_shaped_fixture_flips_abstention_decision_under_v2(monkeypatch):
    """Mirrors attempt 18 (a1-failure-taxonomy.md): 7/7 reference equation
    nodes fully credited, 27 additional over-segmented student nodes (symbol
    glosses / generic procedure filler) of types absent from this synthetic
    problem's candidate set. v1's student-node-wide denominator abstains
    (rate 27/34 > 0.35); v2's structural denominator does not (0/7)."""
    resolved = tuple(_rn(f"r{i}", "resolved") for i in range(7)) + tuple(
        _rn(f"u{i}", "unresolved") for i in range(27)
    )
    resolution = ResolutionResult(resolved=resolved, tier_counts={}, llm_calls=0)
    node_type_by_id = {f"r{i}": "equation" for i in range(7)}
    node_type_by_id.update({f"u{i}": "definition" for i in range(27)})
    candidate_types = frozenset({"equation"})

    v1_rate = unresolved_rate_of(resolution)
    assert round(v1_rate, 4) == round(27 / 34, 4)
    v1_out = apply_abstention(unresolved_rate=v1_rate, min_parser_confidence=1.0)
    assert v1_out.abstained is True
    assert REASON_HIGH_UNRESOLVED in v1_out.abstention_reasons

    monkeypatch.setenv(_ABSTENTION_DENOM_V2_FLAG, "1")
    v2_rate = unresolved_rate_for_abstention(
        resolution, node_type_by_id=node_type_by_id, candidate_types=candidate_types
    )
    assert v2_rate == 0.0
    v2_out = apply_abstention(unresolved_rate=v2_rate, min_parser_confidence=1.0)
    assert v2_out.abstained is False
    assert REASON_HIGH_UNRESOLVED not in v2_out.abstention_reasons
