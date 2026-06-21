"""WU-3B2h — PURE miss-concentration quarantine statistic (fixture-only).

No LLM, no DB, no network, no container. Every ``coverage_matrix`` is a
hand-built ``Mapping[str, Sequence[bool]]`` (node key -> per-attempt "missing?"
flags, all sequences length N). The §8B.7 Tier-3 "mispaired fixture" becomes a
deterministic Tier-1 unit test here, and each of the THREE conjunction clauses
is mutation-proven by a discriminating fixture (drop/loosen the clause -> the
fixture flips). The decision is the pure core; the real-PG sweep is tested
separately in ``tests/database/test_apollo_quarantine_shadow.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.provisioning import quarantine_constants
from apollo.provisioning.quarantine import (
    QuarantineVerdict,
    quarantine_decision,
)

# The committed defaults, used by every fixture unless a test overrides one to
# mutation-prove a clause.
N_MIN = quarantine_constants.N_MIN
THETA = quarantine_constants.THETA_MISS
MARGIN = quarantine_constants.CONCENTRATION_MARGIN


def _flags(missing: int, n: int) -> list[bool]:
    """A length-N flag list with ``missing`` True (missed) then the rest False."""
    return [True] * missing + [False] * (n - missing)


def _decide(matrix, *, n_min=N_MIN, theta=THETA, margin=MARGIN):
    return quarantine_decision(matrix, n_min=n_min, theta_miss=theta, margin=margin)


# ---------------------------------------------------------------------------
# Positive path — the §8B.7 mispaired-reference fixture FIRES.
# ---------------------------------------------------------------------------


def test_fires_on_concentrated_miss():
    """N=10, node ``n_bad`` missed 9/10 (m=0.9 >= 0.80); the other three nodes
    missed 0/1/1 (concentration ~0.625 >= 0.40) -> quarantine fires, the right
    node is named, reason == 'fired'."""
    matrix = {
        "n_bad": _flags(9, 10),
        "n_a": _flags(0, 10),
        "n_b": _flags(1, 10),
        "n_c": _flags(1, 10),
    }
    verdict = _decide(matrix)
    assert verdict.quarantine is True
    assert verdict.top_node_key == "n_bad"
    assert verdict.reason == "fired"
    assert verdict.n_attempts == 10
    assert verdict.top_miss_rate == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# MUTATION-PROOF clause 1 — N >= n_min.
# ---------------------------------------------------------------------------


def test_no_fire_below_n_min():
    """SAME concentrated shape but N=7 (< N_MIN=8) -> does NOT fire,
    reason == 'below_n_min'. Discriminating: drop the N>=n_min clause and this
    fixture would fire (it is concentrated and above theta)."""
    matrix = {
        "n_bad": _flags(7, 7),  # 7/7 missed -> m=1.0 (above theta, concentrated)
        "n_a": _flags(0, 7),
        "n_b": _flags(0, 7),
        "n_c": _flags(1, 7),
    }
    verdict = _decide(matrix)
    assert verdict.quarantine is False
    assert verdict.reason == "below_n_min"
    assert verdict.n_attempts == 7


# ---------------------------------------------------------------------------
# MUTATION-PROOF clause 2 — m(top) >= theta_miss.
# ---------------------------------------------------------------------------


def test_no_fire_below_theta_miss():
    """N=10, the worst node missed 7/10 (m=0.70 < THETA=0.80) but still
    concentrated (others ~0.03, concentration ~0.52 >= 0.40) -> does NOT fire,
    reason == 'below_theta'. Discriminating: loosen THETA to 0.70 and this
    fires (proved below)."""
    matrix = {
        "n_bad": _flags(7, 10),
        "n_a": _flags(0, 10),
        "n_b": _flags(0, 10),
        "n_c": _flags(1, 10),
    }
    verdict = _decide(matrix)
    assert verdict.quarantine is False
    assert verdict.reason == "below_theta"

    # The mutation proof: with THETA loosened to 0.70 the SAME fixture fires.
    fired = _decide(matrix, theta=0.70)
    assert fired.quarantine is True
    assert fired.reason == "fired"


# ---------------------------------------------------------------------------
# MUTATION-PROOF clause 3 — concentration margin (the §9 OPS-4 boundary).
# ---------------------------------------------------------------------------


def test_no_fire_below_concentration_margin():
    """The UNIFORMLY-HARD problem: N=10, EVERY node missed 8/10 (each m=0.80 >=
    THETA) but the top exceeds the mean by 0 (< MARGIN=0.40) -> does NOT fire,
    reason == 'below_margin'. This is the §9 OPS-4 false-positive boundary: a
    genuinely-hard-but-uniform problem must NOT quarantine. Discriminating: drop
    the margin clause and this fires (proved below)."""
    matrix = {
        "n_a": _flags(8, 10),
        "n_b": _flags(8, 10),
        "n_c": _flags(8, 10),
        "n_d": _flags(8, 10),
    }
    verdict = _decide(matrix)
    assert verdict.quarantine is False
    assert verdict.reason == "below_margin"
    assert verdict.top_miss_rate == pytest.approx(0.8)

    # The mutation proof: with MARGIN dropped to 0.0 the SAME fixture fires.
    fired = _decide(matrix, margin=0.0)
    assert fired.quarantine is True
    assert fired.reason == "fired"


# ---------------------------------------------------------------------------
# Empty / degenerate inputs — never divide by zero.
# ---------------------------------------------------------------------------


def test_empty_matrix_does_not_fire_or_divide_by_zero():
    """An empty matrix and a matrix whose sequences are zero-length both produce
    a non-firing 'empty' verdict with NO ZeroDivisionError."""
    empty = _decide({})
    assert empty.quarantine is False
    assert empty.reason == "empty"
    assert empty.n_attempts == 0
    assert empty.top_node_key is None

    zero_len = _decide({"n_a": [], "n_b": []})
    assert zero_len.quarantine is False
    assert zero_len.reason == "empty"
    assert zero_len.n_attempts == 0


# ---------------------------------------------------------------------------
# The verdict DTO — frozen + carries the audit fields the sweep log emits.
# ---------------------------------------------------------------------------


def test_verdict_is_frozen_and_carries_audit_fields():
    matrix = {
        "n_bad": _flags(9, 10),
        "n_a": _flags(0, 10),
        "n_b": _flags(1, 10),
        "n_c": _flags(1, 10),
    }
    verdict = _decide(matrix)
    # All audit fields the sweep's per-fire log emits are populated.
    assert verdict.n_attempts == 10
    assert verdict.top_node_key == "n_bad"
    assert verdict.top_miss_rate == pytest.approx(0.9)
    assert verdict.mean_miss_rate == pytest.approx(0.275)
    assert verdict.concentration == pytest.approx(verdict.top_miss_rate - verdict.mean_miss_rate)
    assert isinstance(verdict, QuarantineVerdict)
    # Frozen: attribute assignment raises.
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.quarantine = False  # type: ignore[misc]


def test_input_matrix_not_mutated():
    """Immutability contract: the input matrix is byte-identical after the call."""
    matrix = {
        "n_bad": [True, True, True, True, True, True, True, True, True, False],
        "n_a": [False] * 10,
    }
    before = {k: list(v) for k, v in matrix.items()}
    _decide(matrix)
    assert matrix == before


def test_constants_are_the_call_defaults_via_sweep_path():
    """Calling with the module constants reproduces the fired verdict — proves the
    sweep's pass-through of N_MIN/THETA_MISS/CONCENTRATION_MARGIN reaches a 'fired'
    verdict on the canonical concentrated fixture."""
    matrix = {
        "n_bad": _flags(9, 10),
        "n_a": _flags(0, 10),
        "n_b": _flags(1, 10),
        "n_c": _flags(1, 10),
    }
    verdict = quarantine_decision(
        matrix,
        n_min=quarantine_constants.N_MIN,
        theta_miss=quarantine_constants.THETA_MISS,
        margin=quarantine_constants.CONCENTRATION_MARGIN,
    )
    assert verdict.quarantine is True
    assert verdict.reason == "fired"
