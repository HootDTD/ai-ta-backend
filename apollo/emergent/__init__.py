"""Emergent per-class misconception store (design memo 2026-07-05, increment 1).

An append-only observation ledger (``apollo_misconception_observations``,
migration 037) fed by ``role='canonical'`` grading artifacts, read back with a
derived-on-read continuous trust score. Everything here is dormant unless the
``APOLLO_EMERGENT_MISCONCEPTIONS`` flag is ON — flag OFF, the write path never
fires and the read path returns nothing, so grading output is byte-identical to
the no-store behavior.

See ``docs/architecture/apollo.md`` (Emergent misconception store) and
``docs/_archive/specs/2026-07-05-emergent-misconception-store-design.md``.
"""

from apollo.emergent.config import (
    K_DISTINCT_STUDENTS,
    RECENCY_HALFLIFE_DAYS,
    TAU_ASSERT,
    TAU_PROJECT,
    UNKEYED_PREFIX,
    emergent_misconceptions_enabled,
)
from apollo.emergent.trust import (
    band,
    is_promotable_signature,
    is_promoted,
    recency_factor,
    trust_score,
)

__all__ = [
    "K_DISTINCT_STUDENTS",
    "RECENCY_HALFLIFE_DAYS",
    "TAU_ASSERT",
    "TAU_PROJECT",
    "UNKEYED_PREFIX",
    "emergent_misconceptions_enabled",
    "band",
    "is_promotable_signature",
    "is_promoted",
    "recency_factor",
    "trust_score",
]
