"""WU-4B2 — the opposes-map builder (a tiny pure adapter over ``Candidate``).

Its own module, exactly as the ``candidates.py`` adapters are pure. The §6.5
conflict rows are detectable only because a misconception entity declares the
entity it opposes (``Candidate.opposes_key``): a CONTRADICTION on the
misconception key + a COVERED on the opposed key is an "opposed pair".
"""

from __future__ import annotations

from collections.abc import Mapping

from apollo.resolution.candidates import Candidate


def build_opposes_map(candidates: tuple[Candidate, ...]) -> Mapping[str, str]:
    """Each misconception candidate's ``canonical_key`` -> the entity it opposes.

    Only misconception candidates with a non-None ``opposes_key`` are included
    (§6.5: opposes-links make the conflict rows detectable). Returns an immutable
    plain dict; a non-misconception or ``opposes_key=None`` candidate contributes
    nothing. The input tuple is never mutated."""
    return {
        c.canonical_key: c.opposes_key
        for c in candidates
        if c.is_misconception and c.opposes_key is not None
    }
