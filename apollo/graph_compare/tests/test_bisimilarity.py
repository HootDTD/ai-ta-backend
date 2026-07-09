"""WU-4A2 Task 5 — bisimilarity = harmonic_mean(soundness, coverage).

RED-first. Binding: returns 0.0 when a+b==0, NEVER NaN (a NaN in a REAL NOT NULL
column poisons aggregates; §6.1).
"""

from __future__ import annotations

import math

import pytest

from apollo.graph_compare.bisimilarity import bisimilarity_score, harmonic_mean


def test_harmonic_mean_typical():
    assert harmonic_mean(1.0, 0.5) == pytest.approx(2 / 3)


def test_harmonic_mean_both_one():
    assert harmonic_mean(1.0, 1.0) == 1.0


def test_harmonic_mean_zero_sum_returns_zero_not_nan():
    result = harmonic_mean(0.0, 0.0)
    assert result == 0.0
    assert not math.isnan(result)


def test_harmonic_mean_one_zero():
    result = harmonic_mean(1.0, 0.0)
    assert result == 0.0  # a+b != 0 but product 0 -> 0; no NaN
    assert not math.isnan(result)


def test_bisimilarity_score_delegates_to_harmonic_mean():
    assert bisimilarity_score(0.8, 0.6) == harmonic_mean(0.8, 0.6)
    assert bisimilarity_score(1.0, 0.0) == 0.0


def test_bisimilarity_na_soundness_renormalizes_to_coverage():
    assert bisimilarity_score(None, 0.8) == 0.8          # coverage-only fallback
    r = bisimilarity_score(None, 0.0)
    assert r == 0.0 and not math.isnan(r)                # still NaN-free, in-range
