"""WU-4A2 — bisimilarity = harmonic_mean(soundness, coverage) (§6.2).

The harmonic mean rewards a graph that is BOTH sound and complete (a high score
requires both dimensions high). The single binding edge case (§6.1): when
``a + b == 0`` the mean is ``0.0``, NEVER NaN — a NaN written to a
``REAL NOT NULL`` column poisons downstream aggregates. There is no other branch.

This score consumes node coverage and contradiction soundness only; it never
traverses DEPENDS_ON edges and is therefore direction-invariant.
"""

from __future__ import annotations


def harmonic_mean(a: float, b: float) -> float:
    """``0.0`` when ``a + b == 0`` else ``2ab/(a+b)``. Never NaN (§6.1)."""
    total = a + b
    if total == 0:
        return 0.0
    return 2 * a * b / total


def bisimilarity_score(soundness: float | None, coverage: float) -> float:
    """``harmonic_mean(soundness, coverage)`` — or, when soundness is N/A
    (``None``; the misconception bank was empty, D5/D6), the coverage-only
    fallback ``bisimilarity == coverage``.

    Rationale (§6.1 + the N/A rule): the harmonic mean of a SINGLE available
    dimension is that dimension. We must NOT substitute ``soundness=1.0``
    (inflates a never-checked answer to a fake-perfect harmonic mean) nor
    ``soundness=0.0`` (zeros a good answer via the product — and ``a+b==0`` does
    NOT catch it when coverage>0). Coverage-only is the only honest top-line when
    soundness was never checked. The result is still NaN-free and in [0, 1], so
    the ``REAL NOT NULL`` column stays safe."""
    if soundness is None:
        return coverage
    return harmonic_mean(soundness, coverage)
