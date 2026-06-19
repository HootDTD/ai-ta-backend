"""WU-5B1 §3 Step 0 — golden-vector tests for the between-session decay core.

Pure imports — no DB, no LLM, no Neo4j, no network (mirrors ``test_belief.py`` /
``test_update.py``). Every expected number below is a HARDCODED golden constant
(the LOCKED vectors from the plan §4, independently reproduced against the frozen
``belief.py`` ``COLD_START_PRIOR``) — NEVER asserted against the function's own
output.

LOCKED: ``DECAY_K=0.05``; ``w = 1 − e^(−k·max(0,dt))``; the convex blend
``(1−w)·belief + w·prior``; half-life ``ln2/k = 13.8629`` days. Do NOT re-derive
or re-parameterize.
"""

from __future__ import annotations

import math

import pytest

from apollo.learner_model.belief import COLD_START_PRIOR
from apollo.learner_model.decay import DECAY_K, decay_toward_prior, decay_weight

# --- LOCKED golden constants (hand-computed, NOT the function's own output) ---
_ZERO_REPAIR_DT3 = (0.02786, 0.08358, 0.88857)  # decay((0,0,1), prior, dt=3)
_W_DT3 = 0.139292  # 1 - e^(-0.05*3) = 1 - e^(-0.15)
_HALF_LIFE = 13.8629  # ln 2 / 0.05


# --- decay_weight ---------------------------------------------------------


def test_decay_weight_dt3_golden():
    # The LOCKED convex-blend weight at dt=3 (hardcoded, not recomputed).
    assert decay_weight(3) == pytest.approx(_W_DT3, abs=1e-6)


def test_decay_weight_dt0_is_zero():
    # dt=0 -> w=0 (no spurious forgetting on a same-day re-attempt). Exact.
    assert decay_weight(0) == 0.0


def test_decay_weight_negative_dt_clamped_to_zero():
    # The max(0,.) clamp kills negative-dt sharpening (clock skew / out-of-order).
    assert decay_weight(-2) == 0.0
    assert decay_weight(-100) == 0.0


def test_decay_weight_monotone_increasing():
    # A bounded convex-blend weight in (0,1), monotone in dt.
    w1, w7, w30 = decay_weight(1), decay_weight(7), decay_weight(30)
    assert w1 < w7 < w30
    for w in (w1, w7, w30):
        assert 0.0 < w < 1.0


def test_half_life_is_ln2_over_k():
    # The LOCKED half-life: ln 2 / k = 13.8629 days; at the half-life w = 0.5.
    assert math.log(2) / DECAY_K == pytest.approx(_HALF_LIFE, abs=1e-4)
    assert decay_weight(_HALF_LIFE) == pytest.approx(0.5, abs=1e-4)


def test_decay_weight_custom_k_override():
    # The k-override param path: decay_weight(10, k=0.1) = 1 - e^(-1.0).
    assert decay_weight(10, k=0.1) == pytest.approx(1 - math.exp(-1.0), abs=1e-9)


# --- decay_toward_prior ---------------------------------------------------


def test_decay_toward_prior_zero_repair_dt3():
    # THE headline golden vector: a zero pulled OFF zero. COLD_START_PRIOR is the
    # frozen belief.py prior (every component >= 0.20 > 0) — assert it equals the
    # hardcoded tuple so the golden vector is bound to the frozen prior, never
    # re-literal'd.
    assert COLD_START_PRIOR == (0.20, 0.60, 0.20)
    out = decay_toward_prior((0.0, 0.0, 1.0), COLD_START_PRIOR, 3)
    # abs=5e-6 is the half-ULP of a 5-decimal-place golden constant: the full-
    # precision value (pinned exactly below) rounds to the LOCKED 5-dp tuple.
    assert out == pytest.approx(_ZERO_REPAIR_DT3, abs=5e-6)
    # Pin the EXACT (hand-computed, plan §4 L155-157) full-precision values so
    # the math is locked, not just the 5-dp rounding.
    assert out == pytest.approx((0.0278584047, 0.0835752141, 0.8885663811), abs=1e-9)


def test_decay_toward_prior_dt0_is_identity():
    # w=0 -> (1-0)*b + 0*prior == b, exactly (no float drift at the identity).
    b = (0.1, 0.3, 0.6)
    assert decay_toward_prior(b, COLD_START_PRIOR, 0) == b


def test_decay_toward_prior_negative_dt_is_identity():
    # The clamp -> w=0 -> identity (out-of-order retry never sharpens).
    b = (0.1, 0.3, 0.6)
    assert decay_toward_prior(b, COLD_START_PRIOR, -2) == b


def test_decay_toward_prior_sums_to_one():
    # A convex blend of two sum-1 vectors stays sum-1 (no renormalize needed).
    b = (0.0, 0.0, 1.0)
    for dt in (1, 3, 7, 14, 30):
        assert sum(decay_toward_prior(b, COLD_START_PRIOR, dt)) == pytest.approx(1.0, abs=1e-9)


def test_decay_toward_prior_never_zero():
    # The §3 'never 0' invariant: decay REPAIRS zeros, cannot create them. Each
    # output component is >= w*0.20 > 0 for dt>0.
    b = (0.0, 0.0, 1.0)
    for dt in (1, 3, 7, 30):
        out = decay_toward_prior(b, COLD_START_PRIOR, dt)
        floor = decay_weight(dt) * 0.20 - 1e-12
        for comp in out:
            assert comp > 0.0
            assert comp >= floor


def test_decay_toward_prior_returns_new_tuple_no_mutation():
    # Immutability: the input belief is unchanged; a NEW 3-tuple is returned.
    b = (0.0, 0.0, 1.0)
    out = decay_toward_prior(b, COLD_START_PRIOR, 5)
    assert b == (0.0, 0.0, 1.0)  # input untouched
    assert out is not b
    assert isinstance(out, tuple)
    assert len(out) == 3


def test_decay_toward_prior_full_decay_approaches_prior():
    # w -> 1 for a very large dt -> the result drifts to the prior ("unknown").
    out = decay_toward_prior((0.0, 0.0, 1.0), COLD_START_PRIOR, 1000)
    assert out == pytest.approx(COLD_START_PRIOR, abs=1e-3)
