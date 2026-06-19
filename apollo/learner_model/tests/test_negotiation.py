"""WU-5B2 §3 — golden-vector tests for the negotiation-move likelihood multiplier.

Pure imports — no DB, no LLM, no Neo4j, no network (mirrors ``test_belief.py`` /
``test_decay.py``). Every expected number below is a HARDCODED golden constant
(the LOCKED vectors from the plan §2, independently reproduced against the frozen
``belief.py`` primitives) — NEVER asserted against the function's own output.

LOCKED: ``NEGOTIATION_LIKELIHOOD=(1.0,1.2,1.1)`` order ``(p_misc,p_shaky,
p_mastered)`` (blank ``L_misc`` = 1.0); ``MULTIPLIER_MOVES={challenge,paraphrase}``
(``skip`` -> no-op identity); the multiplier folds into the COMBINED likelihood
ONCE per entity BEFORE damp; the RATIFIED suppression (adjudication #4b) forces
identity x1.0 on a MISCONCEPTION / PARTIAL-with-code entity so a move never
dilutes a misconception. Do NOT re-derive or re-parameterize.
"""

from __future__ import annotations

import pytest

from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    MISCONCEPTION_LIKELIHOOD,
    NO_OP_LIKELIHOOD,
    _clamp_each,
    _covered_likelihood,
    bayes_update,
    damp,
    mastery_of,
)
from apollo.learner_model.negotiation import (
    MULTIPLIER_MOVES,
    NEGOTIATION_LIKELIHOOD,
    entity_negotiation_move,
    negotiation_multiplier,
    suppresses_hedge,
)

# --- LOCKED golden constants (hand-reproduced, NOT the function's own output) ---
_NEG = (1.0, 1.2, 1.1)  # NEGOTIATION_LIKELIHOOD, order (p_misc, p_shaky, p_mastered)
_NO_OP = (1.0, 1.0, 1.0)
# GV-1 misconception entity + move, the suppression PINS identity:
_MISC_SUPPRESSED = (0.4839, 0.4839, 0.0323)  # p_misc 0.4839 (suppression -> identity)
_MISC_BOOSTED = (0.4399, 0.5279, 0.0323)  # p_misc 0.4399 (no-suppression, distinctness only)
# GV-2 covered@0.6 q=0.7, multiplier BEFORE damp:
_COV_BEFORE_DAMP_MASTERY = 0.5209
_COV_AFTER_DAMP_MASTERY = 0.5232


# --- tiny event builders (frozen dataclass; no DB) ---


def _covered(*, score=0.6, nodes=("n1",)):
    return LearnerEvent(
        canonical_key="eq.continuity",
        event_kind=LearnerEventKind.COVERED,
        score=score,
        evidence_node_ids=nodes,
    )


def _misconception(*, code="misc.x", nodes=("nm",)):
    return LearnerEvent(
        canonical_key="eq.continuity",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code=code,
        evidence_node_ids=nodes,
    )


def _partial(*, code=None, nodes=("np",)):
    return LearnerEvent(
        canonical_key="eq.continuity",
        event_kind=LearnerEventKind.PARTIAL,
        score=0.5,
        misconception_code=code,
        evidence_node_ids=nodes,
    )


def _corrected(*, code="misc.x", nodes=("nc",)):
    return LearnerEvent(
        canonical_key="eq.continuity",
        event_kind=LearnerEventKind.CORRECTED,
        misconception_code=code,
        evidence_node_ids=nodes,
    )


# --- locked constants -------------------------------------------------------


def test_negotiation_likelihood_constant_locked():
    # The LOCKED §3 L428 row, order (p_misc, p_shaky, p_mastered) — blank L_misc=1.0.
    assert NEGOTIATION_LIKELIHOOD == (1.0, 1.2, 1.1)
    assert NEGOTIATION_LIKELIHOOD == _NEG
    # the blank L_misc is the identity 1.0 (the "never 0" component), shaky/mastered boost.
    assert NEGOTIATION_LIKELIHOOD[0] == 1.0
    assert NEGOTIATION_LIKELIHOOD[1] > 1.0
    assert NEGOTIATION_LIKELIHOOD[2] > 1.0


def test_multiplier_moves_locked():
    assert MULTIPLIER_MOVES == frozenset({"challenge", "paraphrase"})
    assert "skip" not in MULTIPLIER_MOVES


# --- negotiation_multiplier -------------------------------------------------


def test_negotiation_multiplier_challenge():
    out = negotiation_multiplier("challenge")
    assert out == _NEG
    assert out is NEGOTIATION_LIKELIHOOD


def test_negotiation_multiplier_paraphrase():
    assert negotiation_multiplier("paraphrase") == _NEG


def test_negotiation_multiplier_skip_is_identity():
    out = negotiation_multiplier("skip")
    assert out == _NO_OP
    assert out is NO_OP_LIKELIHOOD


def test_negotiation_multiplier_none_is_identity():
    assert negotiation_multiplier(None) == _NO_OP


def test_negotiation_multiplier_unknown_is_identity():
    # Defensive: any non-qualifying string -> identity, never raises.
    assert negotiation_multiplier("trace") == _NO_OP


def test_negotiation_multiplier_never_zero():
    for move in ("challenge", "paraphrase", "skip", None):
        for comp in negotiation_multiplier(move):
            assert comp > 0.0


# --- entity_negotiation_move ------------------------------------------------


def test_entity_negotiation_move_picks_node_with_move():
    ev = _covered(nodes=("n1",))
    assert entity_negotiation_move([ev], {"n1": "challenge"}) == "challenge"


def test_entity_negotiation_move_none_when_no_node_negotiated():
    ev = _covered(nodes=("nX",))
    assert entity_negotiation_move([ev], {"n1": "challenge"}) is None


def test_entity_negotiation_move_one_per_entity_no_stack():
    # One event whose three nodes are ALL negotiated -> ONE move string (not a list).
    ev = _covered(nodes=("n1", "n2", "n3"))
    moves = {"n1": "challenge", "n2": "paraphrase", "n3": "skip"}
    out = entity_negotiation_move([ev], moves)
    assert isinstance(out, str)
    assert out in moves.values()


def test_entity_negotiation_move_representative_is_deterministic():
    # Two events; the later (list order) carries the move-bearing node -> that move.
    first = _covered(nodes=("n1",))
    later = _covered(nodes=("n2",))
    moves = {"n2": "paraphrase"}
    assert entity_negotiation_move([first, later], moves) == "paraphrase"


# --- suppresses_hedge -------------------------------------------------------


def test_suppresses_hedge_true_on_misconception():
    assert suppresses_hedge([_misconception()]) is True


def test_suppresses_hedge_true_on_partial_with_code():
    assert suppresses_hedge([_partial(code="misc.x")]) is True


def test_suppresses_hedge_false_on_partial_without_code():
    assert suppresses_hedge([_partial(code=None)]) is False


def test_suppresses_hedge_false_on_corrected():
    # CORRECTED carries a code but the boost is ALLOWED (student fixed it).
    assert suppresses_hedge([_corrected()]) is False


def test_suppresses_hedge_false_on_covered():
    assert suppresses_hedge([_covered()]) is False


def test_suppresses_hedge_true_if_any_event_misconception():
    # any-event semantics: a covered + a misconception on the same entity -> True.
    assert suppresses_hedge([_covered(), _misconception()]) is True


# --- golden math vectors (compose frozen belief.py the way the fold does) ----


def test_gv1_misconception_suppressed_identity_pins_0_4839():
    # Suppression -> NO multiply: combined L = clamp(MISCONCEPTION_LIKELIHOOD).
    combined = _clamp_each(MISCONCEPTION_LIKELIHOOD)
    post = bayes_update(COLD_START_PRIOR, damp(combined, 1.0))
    assert post == pytest.approx(_MISC_SUPPRESSED, abs=5e-5)
    assert post[0] == pytest.approx(0.4839, abs=5e-5)


def test_gv1_misconception_unsuppressed_distinct_proves_cancel():
    # WITHOUT suppression: combined L = MISCONCEPTION_LIKELIHOOD (x) NEGOTIATION.
    combined = tuple(
        a * b
        for a, b in zip(_clamp_each(MISCONCEPTION_LIKELIHOOD), NEGOTIATION_LIKELIHOOD, strict=True)
    )
    post = bayes_update(COLD_START_PRIOR, damp(combined, 1.0))
    assert post == pytest.approx(_MISC_BOOSTED, abs=5e-5)
    assert post[0] == pytest.approx(0.4399, abs=5e-5)
    # the -0.044 p_misc cancel that suppression RESTORES (must not dilute a misconception).
    assert _MISC_SUPPRESSED[0] - _MISC_BOOSTED[0] == pytest.approx(0.0440, abs=5e-4)


def test_gv2_covered_boost_before_damp_mastery_0_5209():
    # Multiplier BEFORE damp: combined L = covered(0.6) (x) NEG, then damp(q=0.7).
    combined = tuple(
        a * b for a, b in zip(_covered_likelihood(0.6), NEGOTIATION_LIKELIHOOD, strict=True)
    )
    post = bayes_update(COLD_START_PRIOR, damp(combined, 0.7))
    assert mastery_of(post) == pytest.approx(_COV_BEFORE_DAMP_MASTERY, abs=5e-5)
    # distinct from the AFTER-damp form -> proves BEFORE-damp is the locked shape.
    assert mastery_of(post) != pytest.approx(_COV_AFTER_DAMP_MASTERY, abs=5e-5)


def test_gv3_never_zero_covered_one_times_neg():
    cov1 = _covered_likelihood(1.0)
    assert cov1 == (0.02, 0.02, 1.0)
    times = tuple(a * b for a, b in zip(cov1, NEGOTIATION_LIKELIHOOD, strict=True))
    assert times == pytest.approx((0.02, 0.024, 1.1), abs=1e-9)
    assert min(times) == pytest.approx(0.02, abs=1e-9)
    assert min(times) > 0.0


def test_gv4_skip_noop_fold_byte_identical():
    # covered@0.6 folded with negotiation_multiplier("skip") == folded with NO_OP.
    base = _covered_likelihood(0.6)
    skip_mult = negotiation_multiplier("skip")
    folded_skip = tuple(a * b for a, b in zip(base, skip_mult, strict=True))
    folded_noop = tuple(a * b for a, b in zip(base, NO_OP_LIKELIHOOD, strict=True))
    assert folded_skip == folded_noop
    post_skip = bayes_update(COLD_START_PRIOR, damp(folded_skip, 0.7))
    post_noop = bayes_update(COLD_START_PRIOR, damp(folded_noop, 0.7))
    assert post_skip == post_noop
