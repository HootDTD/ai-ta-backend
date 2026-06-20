"""WU-3B2h — per-problem anomaly-quarantine thresholds (§8B.3 / ADJ #1).

Three hand-set v1 constants gating the miss-concentration quarantine rule. All
env-overridable for calibration WITHOUT a code change, but the committed
defaults (8 / 0.80 / 0.40) are the PINNED numbers and the tests pin them. The
rule fires for problem P in a course iff ALL THREE hold:

  * ``N >= N_MIN``                                   — enough graded attempts to trust the signal.
  * ``m(top) >= THETA_MISS``                         — one reference node is missed by ~everyone.
  * ``m(top) - mean_m >= CONCENTRATION_MARGIN``      — that miss is CONCENTRATED on one node,
    not the uniform-hard signature (a genuinely-hard problem where every node is missed
    ~equally must NOT quarantine; that is the §9 OPS-4 false-positive boundary).

This is the automated stand-in for a teacher noticing a wrong/mispaired
reference solution. It is ADVISORY until the ADJ #12 calibration step passes
(see ``docs/architecture/apollo.md``). The point-biserial discrimination
refinement (a genuinely-hard node has POSITIVE point-biserial; a mispaired
reference near-zero/negative) is **v1.1** (ADJ #1 / ADJ #8) — NOT built here.
stdlib ``statistics`` only — NO scipy, NO numpy import. No logic, no imports
beyond ``os`` (this module is pure config).
"""

from __future__ import annotations

import os

# Minimum graded attempts of P in a course before the rule can fire.
N_MIN: int = int(os.getenv("APOLLO_QUARANTINE_N_MIN", "8"))

# A single node's miss rate must reach this for the rule to consider it.
THETA_MISS: float = float(os.getenv("APOLLO_QUARANTINE_THETA_MISS", "0.80"))

# The top node's miss rate must exceed the MEAN per-node miss rate by at least
# this much — the "concentrated, not uniformly hard" test.
CONCENTRATION_MARGIN: float = float(
    os.getenv("APOLLO_QUARANTINE_CONCENTRATION_MARGIN", "0.40")
)
