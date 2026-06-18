"""WU-5A1 §3 / §6.5 — the golden-vector heart of the 3-state Bayesian filter.

Pure imports — no DB, no LLM, no Neo4j, no network. Every expected number below
is HAND-COMPUTED (the LOCKED golden vectors from the plan, independently
reproduced with the project interpreter) — NEVER asserted against the function's
own output. ``LearnerEvent``/``LearnerEventKind`` come from
``apollo.grading.event_model`` (frozen WU-4B2); ``AMBIGUOUS_ORDER_SCORE`` from
``apollo.grading.events`` (= 0.5, never re-literal'd).

LOCKED constants pinned here: γ=1.5, floor=0.02 (clamp-each, NOT s-clamp),
cold-start (0.20, 0.60, 0.20), threshold 0.5. Do NOT re-derive or re-parameterize.
"""

from __future__ import annotations

import math

import pytest

from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.grading.events import AMBIGUOUS_ORDER_SCORE
from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    CORRECTED_LIKELIHOOD,
    GAMMA,
    LIKELIHOOD_FLOOR,
    MISCONCEPTION_FLAG_THRESHOLD,
    MISCONCEPTION_LIKELIHOOD,
    MISSING_LIKELIHOOD,
    NO_OP_LIKELIHOOD,
    bayes_update,
    confidence_of,
    damp,
    likelihood_for_event,
    mastery_of,
    misconception_code_of,
)


def _covered(score: float) -> LearnerEvent:
    return LearnerEvent(
        canonical_key="k.x",
        event_kind=LearnerEventKind.COVERED,
        score=score,
    )


def _argmax(t: tuple[float, float, float]) -> int:
    return t.index(max(t))


# --- LOCKED constant pins ------------------------------------------------------


def test_locked_constants():
    assert GAMMA == 1.5
    assert LIKELIHOOD_FLOOR == 0.02
    assert MISCONCEPTION_FLAG_THRESHOLD == 0.5
    assert COLD_START_PRIOR == (0.20, 0.60, 0.20)
    assert NO_OP_LIKELIHOOD == (1.0, 1.0, 1.0)


# --- covered golden vectors ----------------------------------------------------

# (s, expected floored L to 3-dp) — the LOCKED table from the plan.
_COVERED_GOLDEN = [
    (0.00, (1.0000, 0.0200, 0.0200)),
    (0.25, (0.6495, 0.6464, 0.1250)),
    (0.50, (0.3536, 1.0000, 0.3536)),
    (0.75, (0.1250, 0.6464, 0.6495)),
    (1.00, (0.0200, 0.0200, 1.0000)),
]


@pytest.mark.parametrize("score, expected", _COVERED_GOLDEN)
def test_covered_likelihood_golden_vectors(score, expected):
    L = likelihood_for_event(_covered(score))
    for got, want in zip(L, expected):
        assert math.isclose(got, want, abs_tol=1e-3)


def test_covered_argmax_shaky_only_at_half():
    # argmax==shaky (index 1) ONLY at s=0.5.
    assert _argmax(likelihood_for_event(_covered(0.0))) == 0  # misc
    assert _argmax(likelihood_for_event(_covered(0.25))) == 0  # misc, NOT shaky
    assert _argmax(likelihood_for_event(_covered(0.5))) == 1  # shaky
    assert _argmax(likelihood_for_event(_covered(0.75))) == 2  # mastered
    assert _argmax(likelihood_for_event(_covered(1.0))) == 2  # mastered
    # The γ-sensitive inequality: at s=0.25, L_misc strictly beats L_shaky.
    L = likelihood_for_event(_covered(0.25))
    assert L[0] > L[1]


def test_likelihood_floor_applied_at_boundaries():
    # The absorbing-zero kill: at the extremes two components hit the floor.
    L0 = likelihood_for_event(_covered(0.0))
    assert L0[1] == LIKELIHOOD_FLOOR
    assert L0[2] == LIKELIHOOD_FLOOR
    L1 = likelihood_for_event(_covered(1.0))
    assert L1[0] == LIKELIHOOD_FLOOR
    assert L1[1] == LIKELIHOOD_FLOOR
    # No component ever below the floor, across the grid.
    for s in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert all(c >= LIKELIHOOD_FLOOR for c in likelihood_for_event(_covered(s)))


def test_missing_misconception_corrected_likelihoods():
    missing = LearnerEvent(canonical_key="k", event_kind=LearnerEventKind.MISSING)
    misc = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code="misc.x",
    )
    corrected = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.CORRECTED,
        misconception_code="misc.x",
    )
    # The clamp is a no-op on the fixed rows (min component 0.2 > floor 0.02).
    assert likelihood_for_event(missing) == MISSING_LIKELIHOOD == (0.7, 1.0, 0.4)
    assert (
        likelihood_for_event(misc) == MISCONCEPTION_LIKELIHOOD == (3.0, 1.0, 0.2)
    )
    assert (
        likelihood_for_event(corrected) == CORRECTED_LIKELIHOOD == (0.5, 1.5, 1.2)
    )


def test_partial_maps_to_covered_at_half():
    partial = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.PARTIAL,
        score=AMBIGUOUS_ORDER_SCORE,
    )
    Lp = likelihood_for_event(partial)
    Lc = likelihood_for_event(_covered(0.5))
    assert Lp == Lc
    for got, want in zip(Lp, (0.3536, 1.0000, 0.3536)):
        assert math.isclose(got, want, abs_tol=1e-3)
    assert _argmax(Lp) == 1  # shaky


# --- damper invariants ---------------------------------------------------------


def test_damp_zero_is_noop():
    L = likelihood_for_event(_covered(0.25))  # a non-trivial L
    assert damp(L, 0.0) == NO_OP_LIKELIHOOD == (1.0, 1.0, 1.0)


def test_damp_one_is_identity():
    L = likelihood_for_event(_covered(0.25))
    assert damp(L, 1.0) == L


def test_damp_monotone_in_q():
    L = likelihood_for_event(_covered(0.25))  # all components < 1.0
    for i in range(3):
        assert L[i] < 1.0
        # At q=0.3 each component lies strictly between L[i] (q=1) and 1.0 (q=0).
        assert L[i] < damp(L, 0.3)[i] < 1.0
    # Output never 0 (L floored, NO_OP is 1.0).
    assert all(c > 0 for c in damp(L, 0.3))


# --- bayes invariants ----------------------------------------------------------


def test_bayes_update_sums_to_one():
    L = likelihood_for_event(_covered(0.7))
    assert math.isclose(sum(bayes_update(COLD_START_PRIOR, L)), 1.0, abs_tol=1e-9)


def test_bayes_update_zero_sum_guard():
    # All-zero product -> return the prior UNCHANGED (defensive guard).
    assert bayes_update(COLD_START_PRIOR, (0.0, 0.0, 0.0)) == COLD_START_PRIOR


# --- readouts ------------------------------------------------------------------


def test_mastery_of_cold_start_is_half():
    # 0.5 * 0.60 + 0.20 = 0.50 exactly (the FORMULA, not the 0.40 spec prose).
    assert mastery_of(COLD_START_PRIOR) == 0.5


def test_confidence_of_cold_start():
    # 1 - entropy([.2,.6,.2]) / log 3.
    assert math.isclose(confidence_of(COLD_START_PRIOR), 0.135026, abs_tol=1e-4)
    # The 0*log0 guard: a degenerate (1,0,0) is zero entropy -> confidence 1.
    assert confidence_of((1.0, 0.0, 0.0)) == 1.0
    # Max entropy uniform -> confidence 0.
    third = 1.0 / 3.0
    assert math.isclose(confidence_of((third, third, third)), 0.0, abs_tol=1e-9)


# --- two-step misconception flag ----------------------------------------------


def test_misconception_code_below_threshold_returns_none():
    # p_misc is argmax (0.4839) but < 0.5 threshold -> unflagged (None).
    belief = (0.4839, 0.3500, 0.1661)
    assert _argmax(belief) == 0
    event = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code="misc.density_ignored",
    )
    assert misconception_code_of(belief, event) is None


def test_misconception_code_at_threshold_returns_code():
    belief = (0.7475, 0.2000, 0.0525)
    event = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code="misc.density_ignored",
    )
    assert misconception_code_of(belief, event) == "misc.density_ignored"
    # Same belief, but the event carries NO code -> None (nothing to flag).
    no_code = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code=None,
    )
    assert misconception_code_of(belief, no_code) is None


def test_misconception_code_requires_argmax():
    # p_misc >= threshold is NOT enough: it must also be the argmax. Here
    # p_misc=0.5 but p_shaky=0.5 ties and p_mastered makes shaky win? No —
    # choose a belief where p_misc is NOT the max so the gate returns None.
    belief = (0.5, 0.5000001, 0.0)  # p_misc < p_shaky -> NOT argmax
    assert _argmax(belief) != 0
    event = LearnerEvent(
        canonical_key="k",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code="misc.x",
    )
    assert misconception_code_of(belief, event) is None
