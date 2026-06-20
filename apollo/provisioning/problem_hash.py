"""Gate-8 dedup key for the §8B.4 promotion lint (WU-3B2b).

``problem_dup_hash`` is a content-only, deterministic sha256 over a version
prefix + normalized ``problem_text`` + canonical ``given_values`` +
``target_unknown`` (spec §8B.4:1348). It is PURE — stdlib only (``hashlib`` /
``re``), no DB, no LLM, no new package.

Course/concept scoping is the CALLER's job: ``run_promotion_lint`` receives a
BIGINT-concept-scoped ``existing_problem_hashes`` set and tests membership; this
hash never queries the DB. The ``_DUP_HASH_VERSION`` prefix makes a future
normalization change detectable (mirrors ``reference_hash.py``'s
``REFERENCE_HASH_VERSION``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from apollo.schemas.problem import Problem

_DUP_HASH_VERSION = "promotion-dup-v1"


def _normalize_text(text: str) -> str:
    """Collapse internal whitespace, strip, lowercase — so two problems that
    differ only by whitespace/case collide."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _canonical_givens(given_values: Mapping[str, float]) -> str:
    """Render given values order-independently. ``sorted`` kills key-order
    differences; ``repr(float)`` collapses ``2.0``/``2.00`` via float equality."""
    return ",".join(f"{k}={v!r}" for k, v in sorted(given_values.items()))


def problem_dup_hash(problem: Problem) -> str:
    """Gate-8 dedup key: sha256 over normalized text + canonical givens + target.

    Course/concept scoping is the CALLER's job (the BIGINT-concept-scoped set
    passed to ``run_promotion_lint``); this hash is content-only and
    deterministic.
    """
    payload = (
        f"{_DUP_HASH_VERSION}|{_normalize_text(problem.problem_text)}"
        f"|{_canonical_givens(problem.given_values)}|{problem.target_unknown}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
