"""WU-3C2 Step 5 — pure-unit tests for the structural type-compat veto.

No Docker, no network. Pin (§5 step 3):
- type compatibility is a HARD constraint (condition never resolves to an
  equation candidate, even at top text score).

Neighborhood corroboration (§5 steps 2-3) is DEFERRED for v1 (see
``structural.py``): the resolver wires the type-compat HARD constraint +
misconception competition only, so there is no isolation-only neighborhood
helper to test here.
"""

from __future__ import annotations

from apollo.resolution.candidates import Candidate
from apollo.resolution.structural import type_compatible


def _cand(key, *, node_type):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=key,
        opposes_key=None,
    )


def test_type_compatibility_hard_constraint():
    """A condition student node must NEVER resolve to an equation candidate,
    regardless of text score."""
    assert type_compatible("condition", _cand("eq.bernoulli", node_type="equation")) is False
    assert type_compatible("condition", _cand("cond.x", node_type="condition")) is True
