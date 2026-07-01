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
from apollo.resolution.nli_resolution import NLIContext, match_nli_semantic

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
