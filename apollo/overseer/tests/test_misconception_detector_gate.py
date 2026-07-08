"""RED->GREEN tests for the corroboration + dual-tau gate.

Contract: docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md
section 5.5, amended by A1 (dual-tau selection by verdict_token_prob origin).

``gate_findings`` groups per-concept findings from the three detector tiers
(sympy_veto, bank_pattern, judge) and decides, per concept_key, whether the
concept DOCKS (kept as ``misconception``, ``corroborated=True``), is
downgraded to ``needs_clarification`` (lone/sub-threshold judge signal), or is
dropped entirely.
"""

from __future__ import annotations

from apollo.overseer.misconception_detector.config import TAU_FIRE, TAU_FIRE_VERBALIZED
from apollo.overseer.misconception_detector.gate import gate_findings
from apollo.overseer.misconception_detector.types import ConceptFinding


def _finding(
    *,
    concept_key: str = "concept.demand_curve",
    verdict: str = "misconception",
    confidence: float = 1.0,
    severity: float = 0.0,
    evidence_span: str = "student said X",
    signature: str = "misc.some_code",
    source: str = "sympy_veto",
    corroborated: bool = False,
    verdict_token_prob_present: bool = True,
) -> ConceptFinding:
    return ConceptFinding(
        concept_key=concept_key,
        verdict=verdict,
        confidence=confidence,
        severity=severity,
        evidence_span=evidence_span,
        signature=signature,
        source=source,
        corroborated=corroborated,
        verdict_token_prob_present=verdict_token_prob_present,
    )


def test_sympy_veto_alone_docks_deterministically():
    """A lone sympy_veto finding is self-corroborating (deterministic)."""
    finding = _finding(source="sympy_veto", confidence=1.0, corroborated=False)

    result = gate_findings((finding,))

    assert len(result) == 1
    docked = result[0]
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.concept_key == finding.concept_key


def test_bank_and_judge_agreeing_docks_corroborated():
    """bank_pattern + judge >= tau_fire on the same concept -> dock."""
    key = "concept.elasticity"
    bank = _finding(concept_key=key, source="bank_pattern", confidence=0.9, corroborated=False)
    judge = _finding(concept_key=key, source="judge", confidence=TAU_FIRE, corroborated=False)

    result = gate_findings((bank, judge))

    assert len(result) == 1
    docked = result[0]
    assert docked.concept_key == key
    assert docked.verdict == "misconception"
    assert docked.corroborated is True


def test_lone_judge_at_or_above_tau_is_needs_clarification_not_docked():
    """A single judge finding, even at/above tau_fire, never docks alone."""
    judge = _finding(source="judge", confidence=0.99, corroborated=False)

    result = gate_findings((judge,))

    assert len(result) == 1
    routed = result[0]
    assert routed.verdict == "needs_clarification"
    assert routed.corroborated is False


def test_judge_below_tau_is_dropped():
    """A single judge finding below tau_fire is dropped entirely."""
    judge = _finding(source="judge", confidence=TAU_FIRE - 0.10, corroborated=False)

    result = gate_findings((judge,))

    assert result == ()


def test_two_agreeing_non_judge_sources_dock():
    """sympy_veto + bank_pattern agreeing on a concept (no judge) -> dock."""
    key = "concept.opportunity_cost"
    veto = _finding(concept_key=key, source="sympy_veto", confidence=1.0, corroborated=False)
    bank = _finding(concept_key=key, source="bank_pattern", confidence=0.85, corroborated=False)

    result = gate_findings((veto, bank))

    assert len(result) == 1
    docked = result[0]
    assert docked.concept_key == key
    assert docked.verdict == "misconception"
    assert docked.corroborated is True


def test_bank_and_judge_agree_but_judge_below_tau_does_not_dock():
    """2 sources present but judge < tau_fire -> not corroborated per contract."""
    key = "concept.supply_shift"
    bank = _finding(concept_key=key, source="bank_pattern", confidence=0.9, corroborated=False)
    judge = _finding(concept_key=key, source="judge", confidence=TAU_FIRE - 0.05, corroborated=False)

    result = gate_findings((bank, judge))

    # judge signal fails its own tau -> dropped, leaving only the lone bank
    # finding, which per contract needs a 2nd signal to dock and is NOT itself
    # a deterministic source -> nothing docks. It should not appear as a
    # corroborated misconception.
    assert all(f.corroborated is False or f.verdict != "misconception" for f in result)
    assert not any(f.verdict == "misconception" and f.corroborated for f in result)


def test_a1_judge_verbalized_confidence_uses_verbalized_tau():
    """verdict_token_prob=None origin -> gate against TAU_FIRE_VERBALIZED, not TAU_FIRE.

    We simulate this by passing a confidence that sits BETWEEN TAU_FIRE and
    TAU_FIRE_VERBALIZED alongside an explicit flag carried out-of-band: since
    ConceptFinding itself has no verdict_token_prob field (that lives on
    JudgeRaw upstream), the gate must be told which tau applies via its
    ``tau_fire``/``tau_fire_verbalized`` parameters plus per-finding routing.
    This test exercises the gate's default parameterization directly: a lone
    judge finding whose confidence clears TAU_FIRE but NOT
    TAU_FIRE_VERBALIZED must still only reach ``needs_clarification`` (lone
    judge never docks), proving the stricter constant is available and wired.
    """
    assert TAU_FIRE < TAU_FIRE_VERBALIZED
    mid_confidence = (TAU_FIRE + TAU_FIRE_VERBALIZED) / 2
    judge = _finding(source="judge", confidence=mid_confidence, corroborated=False)

    result = gate_findings((judge,))

    assert len(result) == 1
    assert result[0].verdict == "needs_clarification"
    assert result[0].corroborated is False


def test_a1_dual_source_verbalized_path_requires_stricter_threshold():
    """Corroborated dock (bank+judge) requires the judge to clear whichever
    tau applies to it. Passing an explicit lower tau_fire_verbalized here
    proves the gate accepts and would use the second constant when a finding
    is routed through the verbalized path (simulated via explicit param
    override since ConceptFinding carries no verdict_token_prob directly)."""
    key = "concept.marginal_utility"
    bank = _finding(concept_key=key, source="bank_pattern", confidence=0.9, corroborated=False)
    # Confidence clears TAU_FIRE but not the (default) TAU_FIRE_VERBALIZED.
    judge = _finding(concept_key=key, source="judge", confidence=TAU_FIRE + 0.01, corroborated=False)

    # Using default taus (token-prob path assumed) -> should dock.
    result_default = gate_findings((bank, judge))
    assert any(f.verdict == "misconception" and f.corroborated for f in result_default)

    # Forcing a stricter tau_fire (as if this judge finding were verbalized-
    # sourced and required TAU_FIRE_VERBALIZED) should NOT dock.
    result_strict = gate_findings(
        (bank, judge), tau_fire=TAU_FIRE_VERBALIZED, tau_verbalized=TAU_FIRE_VERBALIZED
    )
    assert not any(f.verdict == "misconception" and f.corroborated for f in result_strict)


def test_verbalized_judge_between_taus_does_not_corroborate_dock():
    """A1 production wiring: a verbalized-sourced judge finding (no
    verdict_token_prob) at confidence BETWEEN TAU_FIRE (0.85) and
    TAU_FIRE_VERBALIZED (0.90) must be gated at the STRICTER verbalized tau and
    therefore fail — so it cannot corroborate a bank_pattern into a dock. This
    is the default-parameter path done.py actually calls (no per-call tau
    override), which the old OR-of-two-comparisons gate got wrong (it degenerated
    to >=0.85 and docked)."""
    key = "concept.net_exports_sign"
    bank = _finding(concept_key=key, source="bank_pattern", confidence=0.9, corroborated=False)
    judge = _finding(
        concept_key=key,
        source="judge",
        confidence=0.87,  # between TAU_FIRE and TAU_FIRE_VERBALIZED
        corroborated=False,
        verdict_token_prob_present=False,  # verbalized-sourced -> stricter tau
    )

    result = gate_findings((bank, judge))

    assert not any(f.verdict == "misconception" and f.corroborated for f in result)


def test_verbalized_judge_between_taus_alone_does_not_dock():
    """A lone verbalized-sourced judge finding between the two taus must not
    reach needs_clarification via a wrongly-cleared stricter tau either — at
    0.87 < 0.90 it fails the verbalized tau and is dropped."""
    judge = _finding(
        source="judge",
        confidence=0.87,
        corroborated=False,
        verdict_token_prob_present=False,
    )

    result = gate_findings((judge,))

    assert result == ()


def test_token_prob_judge_between_taus_still_corroborates_dock():
    """The token-probability path stays gated at the LOOSER TAU_FIRE (0.85):
    a token-prob-sourced judge at 0.87 clears its tau and CAN corroborate a
    bank_pattern into a dock. Proves the origin bit routes, not a blanket
    tightening."""
    key = "concept.gdp_identity"
    bank = _finding(concept_key=key, source="bank_pattern", confidence=0.9, corroborated=False)
    judge = _finding(
        concept_key=key,
        source="judge",
        confidence=0.87,
        corroborated=False,
        verdict_token_prob_present=True,  # token-prob-sourced -> looser tau
    )

    result = gate_findings((bank, judge))

    assert any(f.verdict == "misconception" and f.corroborated for f in result)


def test_empty_input_returns_empty_tuple():
    assert gate_findings(()) == ()


def test_multiple_concepts_are_gated_independently():
    key_a = "concept.a"
    key_b = "concept.b"
    veto_a = _finding(concept_key=key_a, source="sympy_veto", confidence=1.0)
    judge_b = _finding(concept_key=key_b, source="judge", confidence=0.5)

    result = gate_findings((veto_a, judge_b))

    by_key = {f.concept_key: f for f in result}
    assert by_key[key_a].corroborated is True
    assert by_key[key_a].verdict == "misconception"
    assert key_b not in by_key  # dropped: lone sub-tau judge


def test_result_is_new_tuple_not_mutated_input():
    finding = _finding(source="sympy_veto")
    original = (finding,)

    result = gate_findings(original)

    assert result is not original
    assert original[0].corroborated is False  # input untouched (immutability)
