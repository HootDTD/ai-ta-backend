"""Tests for the NLI resolver composer (match_nli_semantic) + NLIContext.

Covers:
- Happy path: paraphrase resolves via NLI entailment
- Misconception veto: student paraphasing a misconception → None
- Polarity screen: negation mismatch blocks before NLI is consulted
- Neutral NLI response → None (with adjudicator actually consulted)
- Non-NLI node type (equation) → None without consulting adjudicator
- No adjudicator → None
- Ambiguity guard: two entailing refs within ambiguity_margin → None
- Two entailing refs outside ambiguity_margin → top ref returned
- Contradiction signal: NLI contradiction fires log, result is None
"""

from __future__ import annotations

from apollo.resolution.candidates import Candidate
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult
from apollo.resolution.nli_config import NLIParams
from apollo.resolution.nli_resolution import (
    NLIContext,
    match_nli_misconception_certify,
    match_nli_semantic,
)

ENT = NLIResult("entailment", 0.95, 0.02, 0.03, "fake")
CON = NLIResult("contradiction", 0.02, 0.95, 0.03, "fake")
NEU = NLIResult("neutral", 0.10, 0.05, 0.85, "fake")


def _ref(key: str, name: str) -> Candidate:
    return Candidate(key, -1, "definition", False, None, (), name, None, ())


def _misc(key: str, name: str) -> Candidate:
    return Candidate(key, -1, "definition", True, None, (name,), name, None, ())


# ---------------------------------------------------------------------------
# Starter tests (verbatim from brief with one correction noted below)
# ---------------------------------------------------------------------------


def test_paraphrase_resolves_via_nli(make_def_node):
    node = make_def_node("density stays constant throughout")
    ref = _ref("def.const_density", "density is constant")
    fake = FakeNLIAdjudicator({("density stays constant throughout", "density is constant"): ENT})
    ctx = NLIContext(nli=fake, embedder=None, cache=None, params=NLIParams())
    m = match_nli_semantic(node, (ref,), ctx=ctx)
    assert m is not None and m.method == "nli" and m.candidate.canonical_key == "def.const_density"


def test_misconception_entailment_vetoes(make_def_node):
    node = make_def_node("pressure rises as speed rises")
    ref = _ref("def.tradeoff", "pressure falls as speed rises")
    misc = _misc("misc.same_dir", "pressure rises as speed rises")
    fake = FakeNLIAdjudicator(
        {
            (
                "pressure rises as speed rises",
                "pressure falls as speed rises",
            ): ENT,  # would wrongly credit
            (
                "pressure rises as speed rises",
                "pressure rises as speed rises",
            ): ENT,  # misconception entailed
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_semantic(node, (ref, misc), ctx=ctx) is None  # vetoed


def test_contradiction_unresolved(make_def_node):
    node = make_def_node("energy is not conserved")
    ref = _ref("def.energy", "energy is conserved")
    fake = FakeNLIAdjudicator({("energy is not conserved", "energy is conserved"): CON})
    # polarity rejects before NLI even fires -> None
    assert match_nli_semantic(node, (ref,), ctx=NLIContext(nli=fake, params=NLIParams())) is None


def test_neutral_unresolved(make_def_node):
    """Neutral NLI response returns None; NLI adjudicator must be consulted.

    The student text "density is measured in kilograms" shares the content token
    "density" (len>2) with the ref "density is constant", so the content-overlap
    floor passes and the NLI adjudicator is actually called before returning None.
    """
    node = make_def_node("density is measured in kilograms")
    ref = _ref("def.const_density", "density is constant")

    calls: list[tuple[str, str]] = []

    class _TrackingFake:
        def classify(self, premise: str, hypothesis: str) -> NLIResult:
            calls.append((premise, hypothesis))
            return NEU

    ctx = NLIContext(nli=_TrackingFake(), params=NLIParams())
    assert match_nli_semantic(node, (ref,), ctx=ctx) is None
    assert calls, (
        "NLI adjudicator was never called — content floor blocked before NLI (fix the student text)"
    )


def test_equation_and_variable_mapping_never_entered(make_equation_node):
    node = make_equation_node("P + rho*g*h = const")
    ref = _ref("eq.bernoulli", "bernoulli")
    fake = FakeNLIAdjudicator({})  # must never be consulted
    assert match_nli_semantic(node, (ref,), ctx=NLIContext(nli=fake, params=NLIParams())) is None


def test_inert_without_adjudicator(make_def_node):
    node = make_def_node("anything goes here")
    assert match_nli_semantic(node, (), ctx=NLIContext(nli=None)) is None


# ---------------------------------------------------------------------------
# Extra tests to close coverage gaps
# ---------------------------------------------------------------------------


def test_ambiguity_guard_fires(make_def_node):
    """Two entailing refs within ambiguity_margin → abstain (return None).

    Scores 0.95 and 0.90: diff=0.05 < ambiguity_margin=0.10 → guard fires.
    Covers the ``_LOG.info("nli_ambiguous …")`` branch.
    """
    node = make_def_node("velocity stays constant in a section")
    ref1 = _ref("def.const_vel_a", "velocity is constant")
    ref2 = _ref("def.const_vel_b", "velocity stays unchanged")
    student_text = "velocity stays constant in a section"
    ent_high = NLIResult("entailment", 0.95, 0.02, 0.03, "fake")
    ent_close = NLIResult("entailment", 0.90, 0.02, 0.08, "fake")
    fake = FakeNLIAdjudicator(
        {
            (student_text, "velocity is constant"): ent_high,
            (student_text, "velocity stays unchanged"): ent_close,
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_semantic(node, (ref1, ref2), ctx=ctx) is None


def test_not_ambiguous_resolves_top(make_def_node):
    """Two entailing refs outside ambiguity_margin → highest-score ref returned.

    Scores 0.98 and 0.87: diff≈0.11 > ambiguity_margin=0.10, so the guard does
    NOT fire.  Covers the ``len(passed) >= 2`` path where the winner is returned
    instead of abstaining.  (Using 0.97 instead of 0.98 would hit a float
    precision trap: 0.97 - 0.87 = 0.0999… < 0.10 and the guard would fire.)
    """
    node = make_def_node("speed remains constant throughout flow")
    ref1 = _ref("def.const_speed_a", "speed is constant")
    ref2 = _ref("def.const_speed_b", "speed stays constant")
    student_text = "speed remains constant throughout flow"
    ent_high = NLIResult("entailment", 0.98, 0.02, 0.00, "fake")
    ent_low = NLIResult("entailment", 0.87, 0.02, 0.11, "fake")
    fake = FakeNLIAdjudicator(
        {
            (student_text, "speed is constant"): ent_high,
            (student_text, "speed stays constant"): ent_low,
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams())
    m = match_nli_semantic(node, (ref1, ref2), ctx=ctx)
    assert m is not None and m.candidate.canonical_key == "def.const_speed_a"


def test_contradiction_signal_unresolved(make_def_node):
    """NLI returns CON with high contradiction score → logs signal, result None.

    Polarity allows the pair (no negation/antonym conflict) and content overlap
    passes, so the NLI adjudicator is consulted and returns contradiction.
    Covers the ``_LOG.info("nli_contradiction_signal …")`` branch.
    """
    node = make_def_node("thermal energy spreads through conduction")
    ref = _ref("def.heat_conv", "thermal energy spreads through convection")
    student_text = "thermal energy spreads through conduction"
    fake = FakeNLIAdjudicator(
        {
            (student_text, "thermal energy spreads through convection"): NLIResult(
                "contradiction", 0.02, 0.95, 0.03, "fake"
            ),
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_semantic(node, (ref,), ctx=ctx) is None


def test_veto_polarity_block_and_ref_resolves(make_def_node):
    """Misconception polarity-blocked in veto loop (line 41 ``continue``); ref still resolves.

    The misconception's text negates the student → polarity XOR fires →
    the veto loop ``continue``s without consulting NLI for that misconception.
    The reference then passes all checks and resolves normally.
    """
    node = make_def_node("density stays constant throughout")
    ref = _ref("def.const_dens", "density is constant")
    # Negated form of student's claim → polarity blocks it in the veto loop
    misc = _misc("misc.not_const", "density is not constant")
    student_text = "density stays constant throughout"
    fake = FakeNLIAdjudicator(
        {
            # Only the ref-certify call should fire; NLI for the misc must NOT be consulted
            (student_text, "density is constant"): ENT,
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams())
    m = match_nli_semantic(node, (ref, misc), ctx=ctx)
    assert m is not None and m.candidate.canonical_key == "def.const_dens"


def test_veto_loop_neutral_misc_fallthrough_ref_resolves(make_def_node):
    """Misc shortlisted + polarity passes but NLI neutral → no veto (line 43 False branch).

    The veto ``if`` at line 43 evaluates to False (label != 'entailment'), the
    loop falls through back to line 38 to its end, and the reference is then
    certified normally.
    """
    node = make_def_node("density stays constant in a section")
    ref = _ref("def.const_dens", "density is constant")
    misc = _misc("misc.partial", "density stays constant in some cases")
    student_text = "density stays constant in a section"
    fake = FakeNLIAdjudicator(
        {
            (student_text, "density stays constant in some cases"): NEU,
            (student_text, "density is constant"): ENT,
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams())
    m = match_nli_semantic(node, (ref, misc), ctx=ctx)
    assert m is not None and m.candidate.canonical_key == "def.const_dens"


# ---------------------------------------------------------------------------
# Misconception positive-certify (APOLLO_NLI_MISC_POSITIVE_CERTIFY, default OFF)
# ---------------------------------------------------------------------------
#
# Defect (control issue #82): the misconception branch was VETO-ONLY — an
# utterance entailing a misconception hypothesis >= the veto threshold blocked
# reference credit and returned None, but never positively RESOLVED to the
# misc.* candidate. These tests pin the flag-OFF byte-identity AND the flag-ON
# positive-certify behavior (written red-first against the veto-only branch).


def test_flag_on_misconception_entailment_resolves_to_misc(make_def_node):
    """RED-FIRST: flag ON + misc entailment >= veto threshold -> positive resolve.

    Before the fix this returned None (veto-only); with the flag on the node
    must resolve TO the misc.* candidate with the same ScoredMatch shape the
    reference certify emits (method "nli", cap 0.88)."""
    node = make_def_node("pressure rises as speed rises")
    ref = _ref("def.tradeoff", "pressure falls as speed rises")
    misc = _misc("misc.same_dir", "pressure rises as speed rises")
    fake = FakeNLIAdjudicator(
        {
            ("pressure rises as speed rises", "pressure falls as speed rises"): ENT,
            ("pressure rises as speed rises", "pressure rises as speed rises"): ENT,
        }
    )
    ctx = NLIContext(nli=fake, params=NLIParams(misc_positive_certify=True))
    m = match_nli_semantic(node, (ref, misc), ctx=ctx)
    assert m is not None, "flag ON must positively certify the misconception, not just veto"
    assert m.candidate.canonical_key == "misc.same_dir"
    assert m.candidate.is_misconception
    assert m.method == "nli"
    assert m.score == 0.88  # METHOD_CONFIDENCE_CAP["nli"], same as reference certify


def test_flag_off_misconception_entailment_still_vetoes_to_none(make_def_node):
    """Byte-identity: the EXACT flag-ON scenario with the flag off (explicitly
    False and via the default) still returns None — the veto-only behavior."""
    node = make_def_node("pressure rises as speed rises")
    ref = _ref("def.tradeoff", "pressure falls as speed rises")
    misc = _misc("misc.same_dir", "pressure rises as speed rises")
    fake = FakeNLIAdjudicator(
        {
            ("pressure rises as speed rises", "pressure falls as speed rises"): ENT,
            ("pressure rises as speed rises", "pressure rises as speed rises"): ENT,
        }
    )
    for params in (NLIParams(), NLIParams(misc_positive_certify=False)):
        assert (
            match_nli_semantic(node, (ref, misc), ctx=NLIContext(nli=fake, params=params)) is None
        )


def test_flag_on_below_veto_threshold_unchanged(make_def_node):
    """Misc entailment BELOW the veto threshold -> no veto, no certify, in BOTH
    flag states: the loop falls through and the reference certifies normally."""
    node = make_def_node("density stays constant throughout")
    ref = _ref("def.const_density", "density is constant")
    misc = _misc("misc.const_claim", "density stays constant near walls")
    below = NLIResult("entailment", 0.75, 0.02, 0.23, "fake")  # < veto 0.80
    fake = FakeNLIAdjudicator(
        {
            ("density stays constant throughout", "density stays constant near walls"): below,
            ("density stays constant throughout", "density is constant"): ENT,
        }
    )
    for flag in (False, True):
        m = match_nli_semantic(
            node,
            (ref, misc),
            ctx=NLIContext(nli=fake, params=NLIParams(misc_positive_certify=flag)),
        )
        assert m is not None and m.candidate.canonical_key == "def.const_density"
        assert m.method == "nli"


def test_veto_side_effect_reference_credit_blocked_both_flag_states(make_def_node):
    """A reference that WOULD certify on its own is still never credited when a
    misconception entails >= the veto threshold — in BOTH flag states. Flag off:
    None (pure veto). Flag on: the misc.* resolution (never the reference)."""
    node = make_def_node("density stays constant throughout")
    ref = _ref("def.const_density", "density is constant")
    misc = _misc("misc.const_everywhere", "density stays constant throughout")
    fake = FakeNLIAdjudicator(
        {
            ("density stays constant throughout", "density is constant"): ENT,
            ("density stays constant throughout", "density stays constant throughout"): ENT,
        }
    )
    # Sanity: without the misconception in the candidate set, the ref certifies.
    alone = match_nli_semantic(
        node, (ref,), ctx=NLIContext(nli=fake, params=NLIParams(misc_positive_certify=False))
    )
    assert alone is not None and alone.candidate.canonical_key == "def.const_density"

    off = match_nli_semantic(
        node, (ref, misc), ctx=NLIContext(nli=fake, params=NLIParams(misc_positive_certify=False))
    )
    assert off is None  # veto-only: reference blocked, nothing resolved

    on = match_nli_semantic(
        node, (ref, misc), ctx=NLIContext(nli=fake, params=NLIParams(misc_positive_certify=True))
    )
    assert on is not None and on.candidate.canonical_key == "misc.const_everywhere"
    assert on.candidate.canonical_key != "def.const_density"  # reference still blocked


def test_flag_on_negated_misconception_never_certifies(make_def_node):
    """Bonus/polarity regression: the polarity screen blocks a candidate BEFORE
    NLI is even consulted on a single-negation XOR ('does not rise' vs a
    non-negated hypothesis), so flag ON must never certify to the misc.* key
    when the student NEGATES it. ``FakeNLIAdjudicator({})`` has NO registered
    responses at all — if the polarity guard were bypassed for either the misc
    or the reference candidate, the resulting ``classify()`` call would raise
    KeyError and fail this test, instead of silently returning a wrong match."""
    node = make_def_node("pressure does not rise as speed rises")
    ref = _ref("def.tradeoff", "pressure falls as speed rises")
    misc = _misc("misc.same_dir", "pressure rises as speed rises")
    fake = FakeNLIAdjudicator({})
    ctx = NLIContext(nli=fake, params=NLIParams(misc_positive_certify=True))
    m = match_nli_semantic(node, (ref, misc), ctx=ctx)
    # Both candidates are polarity-blocked (single negation vs none) -> neither
    # is ever passed to NLI -> no match at all, and certainly never the misc key.
    assert m is None


def test_content_overlap_floor_blocks_ref(make_def_node):
    """Ref passes polarity but has no content tokens (len>2) in common with student.

    The content-overlap floor at line 54 fires the ``continue``, NLI is never
    consulted (FakeNLIAdjudicator raises KeyError on any call — the test would
    fail with KeyError if NLI were reached), and None is returned.
    """
    node = make_def_node("density of gas")
    # "friction opposes motion" shares zero content tokens with "density of gas"
    ref = _ref("def.friction", "friction opposes motion")
    fake = FakeNLIAdjudicator({})  # any classify() call would raise KeyError
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_semantic(node, (ref,), ctx=ctx) is None


# ---------------------------------------------------------------------------
# 2026-07 misc-detection routing fixes, Fix 2 — direct guard-path tests for
# match_nli_misconception_certify (the resolver-level integration paths live
# in test_resolver.py; these pin the function's own early-exit guards).
# ---------------------------------------------------------------------------


def test_misc_certify_inert_without_adjudicator_or_miscs(make_def_node):
    """Guard 1: no adjudicator, or an empty misconception candidate set, both
    return None without doing any work."""
    node = make_def_node("density is constant")
    misc = _misc("misc.x", "density is constant")
    no_nli = NLIContext(nli=None, params=NLIParams())
    assert match_nli_misconception_certify(node, (misc,), ctx=no_nli, entailment_bar=0.95) is None
    fake = FakeNLIAdjudicator({})  # any classify() call would raise KeyError
    with_nli = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_misconception_certify(node, (), ctx=with_nli, entailment_bar=0.95) is None


def test_misc_certify_non_nli_node_type_never_consults(make_equation_node):
    """Guard 2: an ``equation`` node (excluded from NLI_NODE_TYPES) returns
    None before the adjudicator is ever consulted."""
    node = make_equation_node("P + rho*g*h = const")
    misc = _misc("misc.x", "P + rho*g*h = const")
    fake = FakeNLIAdjudicator({})  # any classify() call would raise KeyError
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_misconception_certify(node, (misc,), ctx=ctx, entailment_bar=0.95) is None


def test_misc_certify_polarity_screen_blocks_before_nli(make_def_node):
    """Guard 3: a negation-polarity mismatch between the student text and the
    misconception surface trips the polarity pre-screen ``continue`` — the
    adjudicator is never consulted and the function returns None."""
    node = make_def_node("pressure does not drop along the pipe")
    misc = _misc("misc.pressure_drops", "pressure drops along the pipe")
    fake = FakeNLIAdjudicator({})  # any classify() call would raise KeyError
    ctx = NLIContext(nli=fake, params=NLIParams())
    assert match_nli_misconception_certify(node, (misc,), ctx=ctx, entailment_bar=0.95) is None


def test_misc_certify_bar_independent_of_veto_bar(make_def_node):
    """Fix 3 (2026-07 routing fixes), the persona-36 shape: entailment 0.974
    sits ABOVE the certify bar (0.95) but BELOW the veto bar (0.98).

    Flag ON  -> the node positively certifies to the misc.* candidate (the
    certify decision reads ``misc_certify_entailment``, NOT the veto bar).
    Flag OFF -> byte-identical veto-only behavior: 0.974 < 0.98 means NO veto
    fires either, and the node falls through to the reference-certify loop
    (here: resolves to the reference).
    """
    node = make_def_node("nominal gdp essentially the same as real gdp")
    ref = _ref("def.nominal_gdp", "nominal gdp essentially the market-value measure")
    misc = _misc("misc.nominal_for_real", "nominal gdp is the same as real gdp")
    near_miss = NLIResult("entailment", 0.9742, 0.01, 0.0158, "fake")
    fake = FakeNLIAdjudicator(
        {
            (
                "nominal gdp essentially the same as real gdp",
                "nominal gdp is the same as real gdp",
            ): near_miss,
            (
                "nominal gdp essentially the same as real gdp",
                "nominal gdp essentially the market-value measure",
            ): ENT,
        }
    )
    prod_bars = dict(misconception_veto_entailment=0.98, misc_certify_entailment=0.95)

    on = match_nli_semantic(
        node,
        (ref, misc),
        ctx=NLIContext(nli=fake, params=NLIParams(misc_positive_certify=True, **prod_bars)),
    )
    assert on is not None and on.candidate.canonical_key == "misc.nominal_for_real"

    off = match_nli_semantic(
        node,
        (ref, misc),
        ctx=NLIContext(nli=fake, params=NLIParams(misc_positive_certify=False, **prod_bars)),
    )
    assert off is not None and off.candidate.canonical_key == "def.nominal_gdp"


def test_misc_veto_bar_still_vetoes_when_certify_bar_above_it(make_def_node):
    """Defensive ordering: if an operator sets the certify bar ABOVE the veto
    bar, an entailment between the two must still VETO (block reference
    credit) with the flag ON — never silently fall through to reference
    certify."""
    node = make_def_node("pressure rises as speed rises")
    ref = _ref("def.tradeoff", "pressure falls as speed rises")
    misc = _misc("misc.same_dir", "pressure rises as speed rises")
    fake = FakeNLIAdjudicator(
        {
            ("pressure rises as speed rises", "pressure rises as speed rises"): ENT,  # 0.95
            ("pressure rises as speed rises", "pressure falls as speed rises"): ENT,
        }
    )
    params = NLIParams(
        misc_positive_certify=True,
        misconception_veto_entailment=0.90,
        misc_certify_entailment=0.99,
    )
    assert match_nli_semantic(node, (ref, misc), ctx=NLIContext(nli=fake, params=params)) is None
