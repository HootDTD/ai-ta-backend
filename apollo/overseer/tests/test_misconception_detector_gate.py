"""RED->GREEN tests for the corroboration + dual-tau gate (A9-A13 redesign).

Contract: docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md
§4.0 (A13 cross-namespace), §4.4 (truth-table), §5 (full truth-table), §7
(floor-free co-key). Predecessor contract (A1 dual-tau) holds verbatim.

``gate_findings`` groups per-concept findings from the three detector tiers
(sympy_veto, bank_pattern, judge) and decides, per judge/deterministic
concept, whether the concept DOCKS, is downgraded to
``needs_clarification``, or is dropped — per the §5 truth-table. Bank
findings corroborate by validated ``bank_code``, NOT ``concept_key`` (A13) —
the judge tier keys by ``node_id``, the bank tier keys by
``str(concept_id)``, so every co-key test below uses REAL cross-namespace
keys, never a shared synthetic ``concept_key``.
"""

from __future__ import annotations

from apollo.overseer.misconception_detector.config import (
    TAU_FIRE,
    TAU_FIRE_VERBALIZED,
    TAU_SOLO_JUDGE,
)
from apollo.overseer.misconception_detector.gate import (
    _gate_one_concept,
    collect_unkeyed_births,
    gate_findings,
)
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectorSource,
    Verdict,
)


def _finding(
    *,
    concept_key: str = "concept.demand_curve",
    verdict: Verdict = "misconception",
    confidence: float = 1.0,
    severity: float = 0.0,
    evidence_span: str = "student said X",
    signature: str = "misc.some_code",
    source: DetectorSource = "sympy_veto",
    corroborated: bool = False,
    verdict_token_prob_present: bool = True,
    bank_code: str | None = None,
    bank_match_above_floor: bool = True,
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
        bank_code=bank_code,
        bank_match_above_floor=bank_match_above_floor,
    )


# --------------------------------------------------------------------------- #
# Row 1: sympy solo docks, ceiling_eligible=True
# --------------------------------------------------------------------------- #
def test_row1_sympy_solo_docks_ceiling_eligible():
    """A lone sympy_veto finding is self-corroborating (deterministic) and
    trips the ceiling (row 1)."""
    finding = _finding(
        source="sympy_veto",
        confidence=1.0,
        corroborated=False,
        bank_code="sign_flip",
        signature="misc.sign_flip",
    )

    result = gate_findings((finding,))

    assert len(result) == 1
    docked = result[0]
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is True
    assert docked.concept_key == finding.concept_key
    assert docked.signature == "misc.sign_flip"
    assert docked.bank_code == "sign_flip"


# --------------------------------------------------------------------------- #
# Row 2: sympy + anything docks (represented by sympy), ceiling_eligible=True
# --------------------------------------------------------------------------- #
def test_row2_sympy_plus_anything_docks():
    """sympy_veto + a judge on the SAME node -> dock, represented by sympy,
    ceiling-eligible."""
    key = "concept.opportunity_cost"
    veto = _finding(
        concept_key=key,
        source="sympy_veto",
        confidence=1.0,
        corroborated=False,
        bank_code="opp_cost_veto",
        signature="misc.opp_cost_veto",
    )
    judge = _finding(
        concept_key=key,
        source="judge",
        confidence=0.99,
        corroborated=False,
        bank_code=None,
        signature=f"unkeyed:{key}",
    )

    result = gate_findings((veto, judge))

    assert len(result) == 1
    docked = result[0]
    assert docked.concept_key == key
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is True
    assert docked.source == "sympy_veto"
    assert docked.signature == "misc.opp_cost_veto"


# --------------------------------------------------------------------------- #
# Row 3: judge + bank co-key (floor-free, real cross-namespace keys) docks
# under the JUDGE finding, ceiling_eligible=True.
# --------------------------------------------------------------------------- #
def test_row3_judge_bank_cokey_below_floor_docks_under_judge():
    """A13/§7: judge keyed by a node_id-shaped string, bank keyed by
    str(concept_id) — agreement carried ONLY by a shared bank_code. The
    bank's below-floor best match still corroborates (floor-free, L4)."""
    judge = _finding(
        concept_key="node.demand_curve",
        source="judge",
        confidence=0.86,  # >= TAU_FIRE
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
        bank_match_above_floor=True,  # meaningless for judge source
    )
    bank = _finding(
        concept_key="42",  # str(concept_id) — a DIFFERENT namespace
        source="bank_pattern",
        confidence=0.60,  # below BANK_SIM_FLOOR (0.80)
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
        bank_match_above_floor=False,
    )

    result = gate_findings((judge, bank))

    assert len(result) == 1
    docked = result[0]
    # The docked representative is the JUDGE finding (node_id-keyed), NOT the
    # bank finding's str(concept_id) key.
    assert docked.concept_key == "node.demand_curve"
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is True
    assert docked.signature == "misc.includes_transfers"
    # Round-trips the judge finding's own bank_match_above_floor (True),
    # NOT the bank witness's (False) — dataclasses.replace on J, not B.
    assert docked.bank_match_above_floor is True
    assert docked.bank_code == "includes_transfers"


def test_row3_variant_bank_and_judge_agreeing_docks_corroborated():
    """Rewrite of the legacy same-key test as a real row-3 co-key path
    (node.elasticity judge + str(concept_id)-keyed bank sharing bank_code)."""
    judge = _finding(
        concept_key="node.elasticity",
        source="judge",
        confidence=TAU_FIRE,
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.9,
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
        bank_match_above_floor=True,
    )

    result = gate_findings((judge, bank))

    assert len(result) == 1
    docked = result[0]
    assert docked.concept_key == "node.elasticity"
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is True


def test_row3_token_prob_judge_between_taus_still_corroborates_dock():
    """The token-probability path stays gated at the LOOSER TAU_FIRE (0.85):
    a token-prob-sourced judge at 0.87 clears its tau and CAN corroborate a
    co-keyed bank into a dock."""
    judge = _finding(
        concept_key="node.gdp_identity",
        source="judge",
        confidence=0.87,
        corroborated=False,
        verdict_token_prob_present=True,  # token-prob-sourced -> looser tau
        bank_code="c",
        signature="misc.c",
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.9,
        corroborated=False,
        bank_code="c",
        signature="misc.c",
    )

    result = gate_findings((judge, bank))

    assert any(f.verdict == "misconception" and f.corroborated for f in result)


def test_verbalized_judge_between_taus_does_not_corroborate_dock():
    """A verbalized-sourced judge finding (no verdict_token_prob) at
    confidence BETWEEN TAU_FIRE and TAU_FIRE_VERBALIZED must be gated at the
    STRICTER verbalized tau and therefore fail to co-key-dock, even with a
    matching bank_code."""
    judge = _finding(
        concept_key="node.net_exports_sign",
        source="judge",
        confidence=0.87,  # between TAU_FIRE and TAU_FIRE_VERBALIZED
        corroborated=False,
        verdict_token_prob_present=False,  # verbalized-sourced -> stricter tau
        bank_code="net_exports_add",
        signature="misc.net_exports_add",
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.9,
        corroborated=False,
        bank_code="net_exports_add",
        signature="misc.net_exports_add",
    )

    result = gate_findings((judge, bank))

    assert not any(f.verdict == "misconception" and f.corroborated for f in result)


def test_a1_dual_source_verbalized_path_requires_stricter_threshold():
    """Corroborated dock (bank+judge, real cross-namespace co-key) requires
    the judge to clear whichever tau applies to it."""
    judge = _finding(
        concept_key="node.marginal_utility",
        source="judge",
        confidence=TAU_FIRE + 0.01,
        corroborated=False,
        bank_code="mu_code",
        signature="misc.mu_code",
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.9,
        corroborated=False,
        bank_code="mu_code",
        signature="misc.mu_code",
    )

    # Using default taus (token-prob path assumed) -> should dock.
    result_default = gate_findings((judge, bank))
    assert any(f.verdict == "misconception" and f.corroborated for f in result_default)

    # Forcing a stricter tau_fire (as if this judge finding required
    # TAU_FIRE_VERBALIZED) should NOT dock -> row 3b clarify instead.
    result_strict = gate_findings(
        (judge, bank), tau_fire=TAU_FIRE_VERBALIZED, tau_verbalized=TAU_FIRE_VERBALIZED
    )
    assert not any(f.verdict == "misconception" and f.corroborated for f in result_strict)
    assert any(f.verdict == "needs_clarification" for f in result_strict)


# --------------------------------------------------------------------------- #
# Row 3b: judge + bank co-key but judge sub-routed-tau -> clarify, bank
# dropped (no separate output row).
# --------------------------------------------------------------------------- #
def test_row3b_cokey_judge_sub_routed_tau_clarifies():
    judge = _finding(
        concept_key="node.demand_curve",
        source="judge",
        confidence=TAU_FIRE - 0.05,
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.60,
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
        bank_match_above_floor=False,
    )

    result = gate_findings((judge, bank))

    assert len(result) == 1
    routed = result[0]
    assert routed.concept_key == "node.demand_curve"
    assert routed.verdict == "needs_clarification"
    assert routed.corroborated is False
    # No separate dropped/represented row for the "42"-keyed bank witness.
    assert not any(f.concept_key == "42" for f in result)


def test_bank_and_judge_agree_but_judge_below_tau_does_not_dock():
    """2 sources co-keyed but judge < tau_fire -> not corroborated (row 3b)."""
    judge = _finding(
        concept_key="node.supply_shift",
        source="judge",
        confidence=TAU_FIRE - 0.05,
        corroborated=False,
        bank_code="supply_shift_code",
        signature="misc.supply_shift_code",
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.9,
        corroborated=False,
        bank_code="supply_shift_code",
        signature="misc.supply_shift_code",
    )

    result = gate_findings((judge, bank))

    assert all(f.corroborated is False or f.verdict != "misconception" for f in result)
    assert not any(f.verdict == "misconception" and f.corroborated for f in result)


# --------------------------------------------------------------------------- #
# Row 5: lone bank-keyed judge >= TAU_SOLO_JUDGE docks penalty-only
# (ceiling_eligible=False).
# --------------------------------------------------------------------------- #
def test_row5_lone_keyed_judge_solo_docks_penalty_only():
    judge = _finding(
        concept_key="node.demand_curve",
        source="judge",
        confidence=0.95,  # >= TAU_SOLO_JUDGE (0.90)
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
    )

    result = gate_findings((judge,))

    assert len(result) == 1
    docked = result[0]
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is False
    assert docked.signature == "misc.includes_transfers"
    assert docked.concept_key == "node.demand_curve"


def test_row5_boundary_exactly_at_tau_solo_judge_docks():
    judge = _finding(
        source="judge",
        confidence=TAU_SOLO_JUDGE,
        corroborated=False,
        bank_code="c",
        signature="misc.c",
    )

    result = gate_findings((judge,))

    assert len(result) == 1
    assert result[0].verdict == "misconception"
    assert result[0].ceiling_eligible is False


# --------------------------------------------------------------------------- #
# Row 6: lone bank-keyed judge, clears routed tau but sub-solo-tau -> clarify.
# --------------------------------------------------------------------------- #
def test_row6_keyed_judge_sub_solo_tau_clarifies():
    judge = _finding(
        source="judge",
        confidence=0.86,  # clears TAU_FIRE (0.85) but < TAU_SOLO_JUDGE (0.90)
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
    )

    result = gate_findings((judge,))

    assert len(result) == 1
    assert result[0].verdict == "needs_clarification"
    assert result[0].corroborated is False


# --------------------------------------------------------------------------- #
# Row 7: lone UNKEYED judge -> clarify, NEVER docks (even at high confidence).
# --------------------------------------------------------------------------- #
def test_row7_lone_unkeyed_judge_clarifies_never_docks():
    judge = _finding(source="judge", confidence=0.99, corroborated=False, bank_code=None)

    result = gate_findings((judge,))

    assert len(result) == 1
    routed = result[0]
    assert routed.verdict == "needs_clarification"
    assert routed.corroborated is False


def test_lone_judge_at_or_above_tau_is_needs_clarification_not_docked():
    """A single UNKEYED judge finding, even at/above tau_fire, never docks
    alone (row 7)."""
    judge = _finding(source="judge", confidence=0.99, corroborated=False, bank_code=None)

    result = gate_findings((judge,))

    assert len(result) == 1
    routed = result[0]
    assert routed.verdict == "needs_clarification"
    assert routed.corroborated is False


def test_a1_judge_verbalized_confidence_uses_verbalized_tau():
    """verdict_token_prob=None origin -> gate against TAU_FIRE_VERBALIZED,
    not TAU_FIRE. A lone UNKEYED judge finding whose confidence clears
    TAU_FIRE (via the LOOSER token-prob path, i.e. it routes at tau_fire) but
    is NOT bank-keyed must still only reach needs_clarification (row 7 — lone
    unkeyed judge never docks regardless of confidence), proving the routed
    tau selection is wired without conflating it with the dock decision."""
    assert TAU_FIRE < TAU_FIRE_VERBALIZED
    mid_confidence = (TAU_FIRE + TAU_FIRE_VERBALIZED) / 2
    judge = _finding(
        source="judge",
        confidence=mid_confidence,
        corroborated=False,
        bank_code=None,
        verdict_token_prob_present=True,  # token-prob path -> routes at tau_fire
    )

    result = gate_findings((judge,))

    assert len(result) == 1
    assert result[0].verdict == "needs_clarification"
    assert result[0].corroborated is False


def test_a1_judge_verbalized_confidence_between_taus_drops_when_unkeyed():
    """The verbalized-confidence origin bit routes at the STRICTER
    TAU_FIRE_VERBALIZED: the same mid-confidence, but sourced via the
    verbalized fallback (verdict_token_prob_present=False), fails its own
    routed tau and drops (row 8) rather than reaching needs_clarification."""
    mid_confidence = (TAU_FIRE + TAU_FIRE_VERBALIZED) / 2
    judge = _finding(
        source="judge",
        confidence=mid_confidence,
        corroborated=False,
        bank_code=None,
        verdict_token_prob_present=False,
    )

    result = gate_findings((judge,))

    assert result == ()


def test_verbalized_judge_between_taus_alone_does_not_dock():
    """A lone verbalized-sourced judge finding between the two taus must not
    reach needs_clarification via a wrongly-cleared stricter tau either — at
    0.87 < 0.90 it fails the verbalized tau and is dropped (row 8)."""
    judge = _finding(
        source="judge",
        confidence=0.87,
        corroborated=False,
        verdict_token_prob_present=False,
        bank_code=None,
    )

    result = gate_findings((judge,))

    assert result == ()


# --------------------------------------------------------------------------- #
# Row 8: lone judge sub-routed-tau (keyed or not) -> drop.
# --------------------------------------------------------------------------- #
def test_row8_lone_judge_sub_routed_tau_drops():
    judge = _finding(
        source="judge",
        confidence=TAU_FIRE - 0.10,
        corroborated=False,
        bank_code="some_code",
        signature="misc.some_code",
    )

    result = gate_findings((judge,))

    assert result == ()


def test_judge_below_tau_is_dropped():
    """A single UNKEYED judge finding below tau_fire is dropped entirely."""
    judge = _finding(source="judge", confidence=TAU_FIRE - 0.10, corroborated=False, bank_code=None)

    result = gate_findings((judge,))

    assert result == ()


# --------------------------------------------------------------------------- #
# Row 9: lone bank finding (no judge names its code) -> drop, never
# standalone-docks, even well above the floor.
# --------------------------------------------------------------------------- #
def test_row9_lone_bank_no_judge_drops():
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.99,
        corroborated=False,
        bank_code="orphan_code",
        signature="misc.orphan_code",
        bank_match_above_floor=True,
    )

    result = gate_findings((bank,))

    assert result == ()


def test_two_agreeing_non_judge_sources_dock():
    """sympy_veto + bank_pattern on the SAME node (no judge) -> dock,
    represented by sympy (deterministic short-circuit; row 1/2)."""
    key = "concept.opportunity_cost"
    veto = _finding(
        concept_key=key,
        source="sympy_veto",
        confidence=1.0,
        corroborated=False,
        bank_code="opp_cost",
        signature="misc.opp_cost",
    )
    bank = _finding(
        concept_key=key,
        source="bank_pattern",
        confidence=0.85,
        corroborated=False,
        bank_code="opp_cost",
        signature="misc.opp_cost",
    )

    result = gate_findings((veto, bank))

    assert len(result) == 1
    docked = result[0]
    assert docked.concept_key == key
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is True


def test_sympy_veto_alone_docks_deterministically():
    finding = _finding(
        source="sympy_veto",
        confidence=1.0,
        corroborated=False,
        bank_code="veto_code",
        signature="misc.veto_code",
    )

    result = gate_findings((finding,))

    assert len(result) == 1
    docked = result[0]
    assert docked.verdict == "misconception"
    assert docked.corroborated is True
    assert docked.ceiling_eligible is True
    assert docked.concept_key == finding.concept_key


# --------------------------------------------------------------------------- #
# A13/§4.6.1 mandatory guards
# --------------------------------------------------------------------------- #
def test_docked_row_roundtrips_bank_fields_via_replace():
    """A row-3 docked finding round-trips bank_code/signature/
    bank_match_above_floor UNCHANGED from the input JUDGE finding (guards
    against a re-enumeration regression)."""
    judge = _finding(
        concept_key="node.demand_curve",
        source="judge",
        confidence=0.95,
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
        bank_match_above_floor=True,
    )
    bank = _finding(
        concept_key="42",
        source="bank_pattern",
        confidence=0.5,
        corroborated=False,
        bank_code="includes_transfers",
        signature="misc.includes_transfers",
        bank_match_above_floor=False,
    )

    result = gate_findings((judge, bank))

    assert len(result) == 1
    docked = result[0]
    assert docked.bank_code == judge.bank_code
    assert docked.signature == judge.signature
    assert docked.bank_match_above_floor == judge.bank_match_above_floor
    assert docked.concept_key == judge.concept_key


def test_row5_docked_row_roundtrips_bank_fields():
    """Same round-trip guard for the row-5 solo-judge dock path."""
    judge = _finding(
        source="judge",
        confidence=0.95,
        corroborated=False,
        bank_code="c",
        signature="misc.c",
        bank_match_above_floor=True,
    )

    result = gate_findings((judge,))

    assert len(result) == 1
    docked = result[0]
    assert docked.bank_code == "c"
    assert docked.signature == "misc.c"
    assert docked.bank_match_above_floor is True


def test_needs_clarification_never_ceiling_eligible():
    """A clarification row inherits ceiling_eligible=False (tiers never set
    it True pre-gate)."""
    judge = _finding(source="judge", confidence=0.99, corroborated=False, bank_code=None)

    result = gate_findings((judge,))

    assert len(result) == 1
    assert result[0].verdict == "needs_clarification"
    assert result[0].ceiling_eligible is False


# --------------------------------------------------------------------------- #
# Structural / empty / immutability guards
# --------------------------------------------------------------------------- #
def test_empty_input_returns_empty_tuple():
    assert gate_findings(()) == ()


def test_multiple_concepts_are_gated_independently():
    key_a = "concept.a"
    key_b = "concept.b"
    veto_a = _finding(
        concept_key=key_a,
        source="sympy_veto",
        confidence=1.0,
        bank_code="a_code",
        signature="misc.a_code",
    )
    judge_b = _finding(concept_key=key_b, source="judge", confidence=0.5, bank_code=None)

    result = gate_findings((veto_a, judge_b))

    by_key = {f.concept_key: f for f in result}
    assert by_key[key_a].corroborated is True
    assert by_key[key_a].verdict == "misconception"
    assert key_b not in by_key  # dropped: lone sub-tau unkeyed judge


def test_gate_one_concept_no_deterministic_no_judge_returns_none():
    """Defensive guard: ``_gate_one_concept`` is only ever called (via
    ``gate_findings``) with a group anchored by a deterministic or judge
    finding, so a group with neither can't occur in production — but the
    function itself is total and returns None for it, exercised directly."""
    bank_only = ConceptFinding(
        concept_key="42",
        verdict="misconception",
        confidence=0.9,
        severity=0.0,
        evidence_span="x",
        signature="misc.c",
        source="bank_pattern",
        corroborated=False,
        bank_code="c",
    )

    result = _gate_one_concept(
        [bank_only],
        bank_by_code={},
        tau_fire=TAU_FIRE,
        tau_verbalized=TAU_FIRE_VERBALIZED,
        tau_solo=TAU_SOLO_JUDGE,
    )

    assert result is None


def test_result_is_new_tuple_not_mutated_input():
    finding = _finding(source="sympy_veto")
    original = (finding,)

    result = gate_findings(original)

    assert result is not original


# --------------------------------------------------------------------------- #
# collect_unkeyed_births (2026-07-10 emergent misconception map plan, T2 /
# spec §5.3.1, design correction #1). A PURE collector alongside
# ``gate_findings`` — the gate itself stays pure/non-emitting, so a row8
# (sub-tau) drop is otherwise unobservable to any caller. ``collect_unkeyed_
# births`` replays the SAME per-concept anchor grouping + ``_judge_clears_tau``
# + ``best_judge.bank_code is None`` + ``opposes.get(concept_key) is None`` +
# ``verdict in ("wrong", "misconception")`` predicate rows 7/8 use, and
# returns the qualifying ``best_judge`` findings (row7 ∪ row8-that-would-have-
# cleared-tau -- see the tau-floor test below for the deliberate exclusion).
# --------------------------------------------------------------------------- #
def test_collect_unkeyed_births_returns_row7_confident_unkeyed_wrong():
    """A lone unkeyed judge finding that CLEARS routed tau and carries a
    dockable verdict (wrong/misconception) is a birth candidate — this is
    exactly gate.py's row7 (clarify, never docks) input, captured here instead
    of discarded."""
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="wrong",
        confidence=0.95,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((judge,))

    assert births == (judge,)


def test_collect_unkeyed_births_accepts_misconception_verdict_too():
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="misconception",
        confidence=0.95,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((judge,))

    assert births == (judge,)


def test_collect_unkeyed_births_excludes_row8_sub_tau():
    """Row8 (sub-routed-tau unkeyed judge, dropped by the gate) is EXCLUDED
    from births too — the birth must clear the SAME routed tau as a
    dock-eligible finding to be a credible signal. This is the deliberate
    confidence floor (plan T2): a birth is never captured from a hedged,
    low-confidence localization."""
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="wrong",
        confidence=TAU_FIRE - 0.10,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((judge,))

    assert births == ()


def test_collect_unkeyed_births_excludes_clear_verdict_controls():
    """Control-safety (mirrors F-struct's control argument): a clear@1.0
    control finding never births, regardless of confidence — only
    wrong/misconception verdicts qualify."""
    control = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="clear",
        confidence=1.0,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((control,))

    assert births == ()


def test_collect_unkeyed_births_excludes_needs_clarification_verdict():
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="needs_clarification",
        confidence=0.95,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((judge,))

    assert births == ()


def test_collect_unkeyed_births_excludes_bank_keyed_judge():
    """A judge finding that named a bank_code is not "unkeyed" — it is the
    existing dock/clarify machinery's territory, not a birth."""
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="misconception",
        confidence=0.99,
        corroborated=False,
        bank_code="some_code",
        signature="misc.some_code",
    )

    births = collect_unkeyed_births((judge,))

    assert births == ()


def test_collect_unkeyed_births_excludes_struct_opposes_hit():
    """A node whose finding has a struct-opposes match (F-struct) is not a
    birth — the graph already names it, so it docks via the existing co-key
    path, not the emergent map."""
    key = "node.real_basis"
    judge = _finding(
        concept_key=key,
        source="judge",
        verdict="wrong",
        confidence=0.95,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((judge,), opposes_index={key: "nominal_for_real"})

    assert births == ()


def test_collect_unkeyed_births_excludes_sympy_veto_anchored_concept():
    """gate_findings' anchor grouping is BY concept_key across BOTH
    sympy_veto and judge sources (_ANCHOR_SOURCES) -- when a concept_key has
    a sympy_veto finding, `_gate_one_concept` always docks via the
    deterministic branch and the judge finding on that SAME concept_key is
    never independently evaluated (rows 1/2). The collector must replay that
    SAME precedence: a concept_key resolved by a sympy_veto anchor must not
    also birth from an unkeyed confident-wrong judge finding on that same
    concept_key."""
    key = "node.shared_concept"
    veto = _finding(
        concept_key=key,
        source="sympy_veto",
        verdict="misconception",
        confidence=1.0,
        bank_code="some_code",
        signature="misc.some_code",
    )
    judge = _finding(
        concept_key=key,
        source="judge",
        verdict="wrong",
        confidence=0.95,
        corroborated=False,
        bank_code=None,
    )

    births = collect_unkeyed_births((veto, judge))

    assert births == ()

    # Control: the SAME judge finding, without the sympy_veto anchor on its
    # concept_key, still births (proves the exclusion is precedence-driven,
    # not a blanket judge+sympy_veto co-occurrence ban).
    births_without_veto = collect_unkeyed_births((judge,))

    assert len(births_without_veto) == 1
    assert births_without_veto[0].concept_key == key


def test_collect_unkeyed_births_sympy_veto_solo_anchor_no_judge_births_nothing():
    """A concept_key anchored ONLY by a sympy_veto finding (no judge finding
    at all on that concept_key) must not birth -- covers the `judges` empty
    branch reached after a sympy_veto-bearing group is excluded by the
    deterministic-anchor check but genuinely has no judge finding to fall
    back to."""
    veto = _finding(
        concept_key="node.solo_veto",
        source="sympy_veto",
        verdict="misconception",
        confidence=1.0,
        bank_code="solo_code",
        signature="misc.solo_code",
    )

    births = collect_unkeyed_births((veto,))

    assert births == ()


def test_collect_unkeyed_births_excludes_deterministic_and_bank_pattern_sources():
    """Only the judge tier can produce an "unkeyed birth" — sympy_veto always
    self-docks (never unkeyed) and a lone bank_pattern finding never anchors
    its own group (row 9), so neither source contributes births."""
    veto = _finding(
        concept_key="node.a",
        source="sympy_veto",
        verdict="misconception",
        confidence=1.0,
        bank_code="a_code",
        signature="misc.a_code",
    )
    bank_only = _finding(
        concept_key="42",
        source="bank_pattern",
        verdict="misconception",
        confidence=0.9,
        bank_code="c",
        signature="misc.c",
    )

    births = collect_unkeyed_births((veto, bank_only))

    assert births == ()


def test_collect_unkeyed_births_multiple_concepts_independent():
    """Mirrors gate_findings' per-concept independence: a births collector
    over multiple concepts returns one entry per qualifying concept."""
    key_a, key_b = "node.a", "node.b"
    birth_a = _finding(
        concept_key=key_a, source="judge", verdict="wrong", confidence=0.95, bank_code=None
    )
    dock_b = _finding(
        concept_key=key_b,
        source="sympy_veto",
        verdict="misconception",
        confidence=1.0,
        bank_code="b_code",
        signature="misc.b_code",
    )
    clear_c = _finding(
        concept_key="node.c", source="judge", verdict="clear", confidence=1.0, bank_code=None
    )

    births = collect_unkeyed_births((birth_a, dock_b, clear_c))

    assert len(births) == 1
    assert births[0].concept_key == key_a


def test_collect_unkeyed_births_empty_input_returns_empty_tuple():
    assert collect_unkeyed_births(()) == ()


def test_collect_unkeyed_births_verbalized_confidence_uses_verbalized_tau():
    """Dual-tau routing (A1) is replayed identically: a verbalized-origin
    finding must clear the STRICTER TAU_FIRE_VERBALIZED, not the looser
    TAU_FIRE, to qualify as a birth."""
    mid_confidence = (TAU_FIRE + TAU_FIRE_VERBALIZED) / 2
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="wrong",
        confidence=mid_confidence,
        corroborated=False,
        bank_code=None,
        verdict_token_prob_present=False,  # routes at the stricter tau
    )

    births = collect_unkeyed_births((judge,))

    assert births == ()


def test_collect_unkeyed_births_does_not_mutate_or_alias_input():
    judge = _finding(
        concept_key="node.real_basis",
        source="judge",
        verdict="wrong",
        confidence=0.95,
        bank_code=None,
    )
    original = (judge,)

    births = collect_unkeyed_births(original)

    assert births is not original
    assert births[0] is judge  # findings themselves are returned verbatim (no replace)
    assert original[0].corroborated is False  # input untouched (immutability)
