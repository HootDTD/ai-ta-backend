"""WU-4A2 — bisimilarity = harmonic_mean(soundness, coverage) (§6.2).

The harmonic mean rewards a graph that is BOTH sound and complete (a high score
requires both dimensions high). The single binding edge case (§6.1): when
``a + b == 0`` the mean is ``0.0``, NEVER NaN — a NaN written to a
``REAL NOT NULL`` column poisons downstream aggregates. There is no other branch.
"""

from __future__ import annotations


def harmonic_mean(a: float, b: float) -> float:
    """``0.0`` when ``a + b == 0`` else ``2ab/(a+b)``. Never NaN (§6.1)."""
    total = a + b
    if total == 0:
        return 0.0
    return 2 * a * b / total


def bisimilarity_score(soundness: float, coverage: float) -> float:
    """The top-line bisimilarity: ``harmonic_mean(soundness, coverage)``."""
    return harmonic_mean(soundness, coverage)
