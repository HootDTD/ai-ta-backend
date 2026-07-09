"""Flag + calibration constants for the Apollo misconception detector.

The flag ``APOLLO_MISCONCEPTION_DETECTOR`` defaults **OFF**. While OFF,
``handle_done`` and ``build_llm_artifact`` are byte-identical to today
(penalty 0.0, ``misconceptions: []``, composite unchanged) — see the plan's
design invariant #1.

Pattern mirrors ``apollo/emergent/config.py`` (call-time flag read, same
``_TRUTHY`` set) and ``apollo/grading/composite.py`` (env-overridable float
constants, malformed value falls back to the default).

Calibration constants (A1/R2, pre-calibration defaults — favor false-negative
over false-positive until tuned against the labeled replay batch):

  * ``TAU_FIRE`` (0.85): confidence gate for judge findings whose confidence
    came from a real verdict-token probability.
  * ``TAU_FIRE_VERBALIZED`` (0.90): stricter gate for judge findings whose
    confidence came from the verbalized-confidence fallback field (no
    logprob backing — verbalized confidence tends to run overconfident).
  * ``SEVERITY_CLAMP`` (0.30): max total misconception penalty subtracted
    from the composite.
  * ``CENTRALITY_W_MIN`` (0.30): floor so a peripheral reference node still
    carries some centrality weight.
  * ``CEILING_COMPOSITE`` (0.84): scorecard-projection ceiling applied when a
    central corroborated misconception fires (below the named Strong band,
    A4 — this constant is consumed ONLY by ``apply.py::apply_penalty``).
  * ``BANK_SIM_FLOOR`` (0.80): minimum cosine similarity for a bank_pattern
    match to fire as a STANDALONE finding (L4, corroboration/keying redesign
    spec §4.2). It no longer gates whether a below-floor best match may
    *corroborate* a co-keyed judge finding (gate.py's floor-free co-key path,
    A10) — only whether the match stands on its own.
  * ``TAU_SOLO_JUDGE`` (0.90): minimum routed-tau confidence for a LONE
    bank-keyed judge finding to dock on its own (A9). See the constant's own
    docstring below.
"""

from __future__ import annotations

import os

FLAG_ENV: str = "APOLLO_MISCONCEPTION_DETECTOR"
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def detector_enabled() -> bool:
    """True iff ``APOLLO_MISCONCEPTION_DETECTOR`` is set to a truthy value.

    Read at call time (never cached) so tests can flip it per-case and a
    deploy can flip it without a restart, matching
    ``apollo/emergent/config.py::emergent_misconceptions_enabled``.
    """
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUTHY


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Calibration knobs — env-overridable, conservative defaults (favor FN over
# FP; R3). Read at import time, mirroring apollo/emergent/config.py's OQ2
# constants: these are tuning constants, not per-request state.
TAU_FIRE: float = _float_env("APOLLO_MISC_TAU_FIRE", 0.85)
TAU_FIRE_VERBALIZED: float = _float_env("APOLLO_MISC_TAU_FIRE_VERBALIZED", 0.90)
SEVERITY_CLAMP: float = _float_env("APOLLO_MISC_SEVERITY_CLAMP", 0.30)
CENTRALITY_W_MIN: float = _float_env("APOLLO_MISC_CENTRALITY_MIN", 0.30)
CEILING_COMPOSITE: float = _float_env("APOLLO_MISC_CEILING", 0.84)
BANK_SIM_FLOOR: float = _float_env("APOLLO_MISC_BANK_SIM", 0.80)

# A9 (corroboration/keying redesign spec §4.2): minimum routed-tau confidence
# for a LONE bank-keyed judge finding to dock on its own (no second tier).
# Deliberately >= TAU_FIRE so a solo dock is strictly harder than a
# corroborated one. Pre-calibration default 0.90; MUST be tuned on the
# labeled 20-set before any env flip (R2). Applies ON TOP of the A1
# routed-tau check (the finding must clear BOTH its routed tau AND this).
TAU_SOLO_JUDGE: float = _float_env("APOLLO_MISC_TAU_SOLO_JUDGE", 0.90)
