"""WU-4A2 — soundness pass: S_norm ⊑ R_norm, CONTRADICTIONS ONLY (§6.2).

Soundness measures whether the student's canonical graph asserts anything that
CONTRADICTS the reference. The binding rule (§6.2): only contradictions are
penalized. A contradiction is detected STRUCTURALLY (never by LLM judgment): an
S_norm :class:`CanonicalNode` whose ``canonical_key`` starts with
:data:`MISCONCEPTION_KEY_PREFIX` (``"misc."``). Misconceptions already competed
at resolve-time (§5, WU-3C2), so a node reaching S_norm with a ``misc.*`` key IS
a resolved contradiction.

**Unsupported extras** (S_norm nodes whose key matches no reference path and is
not a misconception — e.g. a valid assumption the reference omits) and
**unresolved nodes** carry ZERO soundness penalty. A wrong-but-unenumerated
claim that does NOT resolve to a ``misc.*`` key is therefore NOT a contradiction
(honest non-detection; §6.11).

Key-reality note: the spec prose says ``canon.misc.*``; the actual minted key
prefix (verified in ``misconceptions.json`` — ``misc.density_ignored`` etc.) is
``misc.``. The single :data:`MISCONCEPTION_KEY_PREFIX` constant documents this
once. The misconception competition's ``opposes`` link is needed only for the
§6.5 turn-order event rows (WU-4B); WU-4A2 emits a ``contradiction`` FINDING and
never needs ``opposes``, so ``grade_attempt`` does not receive the Candidate set.

The unit penalty is a documented v1 calibration constant (NOT "TBD") and is
exactly ``0.5`` here. Pure + deterministic.
"""

from __future__ import annotations

from apollo.graph_compare.canonical import CanonicalGraph, CanonicalNode

# The minted misconception key prefix (the chokepoint for contradiction
# detection). Spec prose `canon.misc.*` maps to the actual `misc.*` mint.
MISCONCEPTION_KEY_PREFIX: str = "misc."

# v1 calibration knob (§6.6 "hand-set v1"): each contradiction subtracts this
# from soundness, capped so 2+ contradictions floor soundness at 0.0.
CONTRADICTION_UNIT_PENALTY: float = 0.5


def is_misconception_key(key: str) -> bool:
    """True iff ``key`` is a minted misconception key (prefix ``misc.``)."""
    return key.startswith(MISCONCEPTION_KEY_PREFIX)


def contradiction_nodes(student: CanonicalGraph) -> tuple[CanonicalNode, ...]:
    """The S_norm nodes whose ``canonical_key`` is a misconception key (the
    resolved contradictions). Order preserved from ``student.nodes``."""
    return tuple(n for n in student.nodes if is_misconception_key(n.canonical_key))


def contradiction_penalty(n: int) -> float:
    """Linear-capped penalty: ``min(1.0, n * CONTRADICTION_UNIT_PENALTY)``.

    Anchors (binding): 0 -> 0.0, 1 -> 0.5, 2+ -> 1.0. Monotone non-decreasing."""
    return min(1.0, n * CONTRADICTION_UNIT_PENALTY)


def soundness_score(student: CanonicalGraph) -> float:
    """``1 - contradiction_penalty(#contradictions)``.

    Unsupported extras and unresolved nodes contribute ZERO penalty (they are not
    contradictions). Empty student graph -> 0 contradictions -> 1.0 (vacuously
    sound; §6.1)."""
    return 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
