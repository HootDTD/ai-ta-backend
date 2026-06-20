"""WU-5A1 §3 / §6.5 — the PURE 3-state Bayesian belief filter (the math core).

This unit is PURE: NO DB, NO LLM, NO Neo4j, NO containers, NO network. It owns
EVERY number in the §3 belief filter and nothing else: the cold-start prior, the
floored §6.5 likelihood table, the §3.1 linear damper, Bayes+renormalize, and the
mastery/confidence/misconception readouts. The belief vector is
``(p_misc, p_shaky, p_mastered)`` throughout.

Mirrors the ``apollo/grading/events.py`` pure-module convention (``import math``,
named module constants, NO IO). Imports only ``apollo.grading.event_model``
(``LearnerEvent``/``LearnerEventKind`` — type dispatch) and
``apollo.grading.events.AMBIGUOUS_ORDER_SCORE`` (the partial->covered@0.5 score —
never re-literal'd).

LOCKED — do NOT re-derive, re-improve, or re-parameterize: γ=1.5, the floor 0.02
(clamp-EACH-component, NOT an s-clamp), the cold-start vector, the fixed
likelihood rows, the mastery/confidence formulas, and the 0.5 flag threshold.

The floor (``LIKELIHOOD_FLOOR``) is the BKT boundary-floor implementing §3 line
427 "never 0": without it ``L_covered(1.0) = (0, 0, 1)`` would permanently
annihilate ``p_misc`` (the absorbing-zero), so a learner who once explained
perfectly could never again be flagged for a misconception.
"""

from __future__ import annotations

import math

from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.grading.events import AMBIGUOUS_ORDER_SCORE

# Cold-start belief (p_misc, p_shaky, p_mastered). mastery_of == 0.50 (the
# formula); the spec §3 prose "0.40" is an arithmetic error (corrected in the
# spec commit).
COLD_START_PRIOR: tuple[float, float, float] = (0.20, 0.60, 0.20)

# Covered-likelihood sharpness. γ=1.5 makes s=0.5 -> argmax shaky and s=0.25 ->
# argmax misc (intended below s≈0.4). LOCKED.
GAMMA: float = 1.5

# BKT boundary-floor: each likelihood component is clamped to >= this BEFORE the
# damper. Kills the absorbing-zero at the covered extremes s∈{0,1}. LOCKED.
LIKELIHOOD_FLOOR: float = 0.02

# A misconception is FLAGGED (its code surfaced) only once p_misc is the argmax
# AND at/above this threshold — typically only on the 2nd misconception. LOCKED.
MISCONCEPTION_FLAG_THRESHOLD: float = 0.5

# Fixed (event-kind) likelihood rows. The floor is a no-op on these (min
# component 0.2 > floor) — clamped anyway for uniform invariance.
MISSING_LIKELIHOOD: tuple[float, float, float] = (0.7, 1.0, 0.4)
MISCONCEPTION_LIKELIHOOD: tuple[float, float, float] = (3.0, 1.0, 0.2)
CORRECTED_LIKELIHOOD: tuple[float, float, float] = (0.5, 1.5, 1.2)

# The damper's "no evidence" anchor: q=0 collapses any L to this no-op.
NO_OP_LIKELIHOOD: tuple[float, float, float] = (1.0, 1.0, 1.0)


def _clamp_each(L: tuple[float, float, float]) -> tuple[float, float, float]:
    """Clamp each component to ``>= LIKELIHOOD_FLOOR`` (the BKT boundary floor)."""
    return (
        max(L[0], LIKELIHOOD_FLOOR),
        max(L[1], LIKELIHOOD_FLOOR),
        max(L[2], LIKELIHOOD_FLOOR),
    )


def _covered_likelihood(s: float) -> tuple[float, float, float]:
    """The §6.5 covered row at coverage ``s`` (already resolution-scaled by the
    converter — NEVER re-scaled here), floored per component."""
    return _clamp_each(
        (
            (1 - s) ** GAMMA,
            1 - abs(2 * s - 1) ** GAMMA,
            s**GAMMA,
        )
    )


def likelihood_for_event(event: LearnerEvent) -> tuple[float, float, float]:
    """The §6.5 likelihood row for ``event`` (floored). Dispatches on
    ``event.event_kind``."""
    kind = event.event_kind
    if kind == LearnerEventKind.COVERED:
        # event.score is the resolution-scaled coverage — use it verbatim.
        # COVERED events always carry a score (converter contract).
        assert event.score is not None, "COVERED event missing score"
        return _covered_likelihood(event.score)
    if kind == LearnerEventKind.PARTIAL:
        # The converter already set partial's score to 0.5; map via the imported
        # constant for provenance (never re-literal 0.5). == covered@0.5.
        return _covered_likelihood(AMBIGUOUS_ORDER_SCORE)
    if kind == LearnerEventKind.MISSING:
        return _clamp_each(MISSING_LIKELIHOOD)
    if kind == LearnerEventKind.MISCONCEPTION:
        return _clamp_each(MISCONCEPTION_LIKELIHOOD)
    # CORRECTED — the last kind in LearnerEventKind.
    return _clamp_each(CORRECTED_LIKELIHOOD)


def damp(L: tuple[float, float, float], q: float) -> tuple[float, float, float]:
    """§3.1 linear damper: ``q·L + (1-q)·(1,1,1)``. q=0 -> NO_OP (no evidence);
    q=1 -> identity. Output is never 0 (L floored, NO_OP is 1.0)."""
    a, b, c = (q * li + (1 - q) * one for li, one in zip(L, NO_OP_LIKELIHOOD, strict=True))
    return a, b, c


def bayes_update(
    prior: tuple[float, float, float], L: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Bayes update + renormalize: ``normalize(prior ⊙ L)``. Zero-sum guard: if
    the products sum to 0 (degenerate input), return ``prior`` unchanged."""
    products = tuple(p * li for p, li in zip(prior, L, strict=True))
    z = sum(products)
    if z == 0:
        return prior
    a, b, c = (x / z for x in products)
    return a, b, c


def mastery_of(belief: tuple[float, float, float]) -> float:
    """Scalar mastery readout: ``0.5·p_shaky + p_mastered`` (§3). Cold-start
    -> 0.50."""
    return 0.5 * belief[1] + belief[2]


def confidence_of(belief: tuple[float, float, float]) -> float:
    """Scalar confidence readout: ``1 - normalized_entropy`` (entropy in nats /
    ``log 3``). The ``if p > 0`` filter is the ``0·log0 == 0`` guard."""
    entropy = -sum(p * math.log(p) for p in belief if p > 0)
    return 1 - entropy / math.log(3)


def misconception_code_of(belief: tuple[float, float, float], event: LearnerEvent) -> str | None:
    """Two-step misconception flag: return ``event.misconception_code`` ONLY when
    ``p_misc`` is the argmax AND ``>= MISCONCEPTION_FLAG_THRESHOLD`` AND the event
    carries a code; else ``None``. LOCKED tie rule: ``belief[0] == max(belief)``
    counts as argmax (harmless given the threshold gate)."""
    if event.misconception_code is None:
        return None
    if belief[0] != max(belief):
        return None
    if belief[0] < MISCONCEPTION_FLAG_THRESHOLD:
        return None
    return event.misconception_code
