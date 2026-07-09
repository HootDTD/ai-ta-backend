"""WU-3B2c — course-local dedup ladder thresholds (§8B.5 / ADJ #4).

Config-driven, calibration-tunable. The defaults are the PINNED numbers
(ADJ #4); an optional env override lets ops recalibrate WITHOUT a code change,
but the committed defaults must stay 0.92 / 0.82 so the routing tests pin them.

Band semantics (lower-inclusive, upper-exclusive):

  * ``cos >= EMBED_MERGE_THRESHOLD``            -> merge on the embedding tier
  * ``BAND_LOW <= cos < EMBED_MERGE_THRESHOLD`` -> escalate to the LLM judge
  * ``cos < BAND_LOW``                          -> distinct on the embedding tier

No logic, no imports beyond ``os`` (this module is pure config).
"""

from __future__ import annotations

import os

# >= this cosine merges outright on the embedding tier (inclusive boundary).
EMBED_MERGE_THRESHOLD: float = float(os.getenv("APOLLO_DEDUP_MERGE_THRESHOLD", "0.92"))

# Lower bound of the escalate-to-judge band (== band lower bound, named for
# readability at the call site). A cosine strictly below this is distinct.
_DISTINCT_BELOW: float = float(os.getenv("APOLLO_DEDUP_JUDGE_BAND_LOW", "0.82"))

# The escalate-to-judge band: lower-inclusive, upper-exclusive.
# ``_DISTINCT_BELOW <= cos < EMBED_MERGE_THRESHOLD`` -> LLM-judge tiebreaker.
EMBED_JUDGE_BAND: tuple[float, float] = (_DISTINCT_BELOW, EMBED_MERGE_THRESHOLD)
