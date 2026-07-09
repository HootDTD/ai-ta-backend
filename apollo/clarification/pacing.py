"""Pacing for the clarification loop (spec §6.5): at most 3 follow-ups per turn,
one per idea, prioritized by rubric weight then detector cosine."""

from __future__ import annotations

from apollo.clarification.detector import FlaggedNode
from apollo.overseer.rubric import AXIS_WEIGHTS, _axis_for

MAX_PROBES_PER_TURN = 3


def rubric_weight_for(node_type: str) -> float:
    """The graded-axis weight for a node type (0.0 for ungraded types — those
    fall back to cosine ordering)."""
    axis = _axis_for(node_type)
    if axis is None:
        return 0.0
    return AXIS_WEIGHTS.get(axis, 0.0)


def select_probes(
    flagged: list[FlaggedNode], *, limit: int = MAX_PROBES_PER_TURN
) -> list[FlaggedNode]:
    """One per idea (dedupe by node_id, keeping the strongest cosine), then the
    top ``limit`` by (rubric weight desc, cosine desc)."""
    best_by_node: dict[str, FlaggedNode] = {}
    for f in flagged:
        cur = best_by_node.get(f.node.node_id)
        if cur is None or f.cosine > cur.cosine:
            best_by_node[f.node.node_id] = f
    ordered = sorted(
        best_by_node.values(),
        key=lambda f: (rubric_weight_for(f.node.node_type), f.cosine),
        reverse=True,
    )
    return ordered[:limit]
