"""WU-3C2 — bounded greedy global assignment (§5 step 4).

The mapping is solved jointly to maximize total match score, but bounded:
greedy assignment in descending match-score order is sufficient at v1 scale
(~15-25 candidates). Many student nodes MAY merge into one reference node
(paraphrase evidence converges); one student node NEVER splits across several
targets (it takes its single best). The student-node count is capped
(:data:`MAX_STUDENT_NODES`) and a pathological graph over the cap is routed to
ABSTENTION rather than letting an unbounded solve hang the Done path.

Pure + deterministic: ties break on ``(node_id, canonical_key)`` so two runs on
the same input produce identical assignments.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apollo.resolution.structural import ScoredMatch

# §5 bounded-assignment cap. At or under this many student nodes the greedy
# assignment runs; strictly over it the whole attempt abstains.
MAX_STUDENT_NODES = 150


@dataclass(frozen=True)
class AssignmentOutcome:
    """Result of :func:`greedy_global_assignment`.

    ``assignment`` maps each resolved student ``node_id`` to its winning
    :class:`ScoredMatch` (one per node — never a split). ``abstained`` is True
    when the attempt exceeded :data:`MAX_STUDENT_NODES` (then ``assignment`` is
    empty)."""

    assignment: dict[str, ScoredMatch] = field(default_factory=dict)
    abstained: bool = False


def greedy_global_assignment(
    matches_by_node: dict[str, list[ScoredMatch]],
    *,
    cap: int = MAX_STUDENT_NODES,
) -> AssignmentOutcome:
    """Assign each student node its single best candidate, greedily in
    descending score order.

    - Over ``cap`` student nodes -> abstain (empty assignment, ``abstained``).
    - Each node keeps at most ONE match (its highest score; deterministic
      tie-break on ``(node_id, canonical_key)``) — one student never splits.
    - Many nodes MAY share one candidate (merge / paraphrase) — that is allowed
      and intentional; candidates are not consumed.
    """
    if len(matches_by_node) > cap:
        return AssignmentOutcome(assignment={}, abstained=True)

    # Pick each node's single best match (deterministic tie-break).
    per_node_best: dict[str, ScoredMatch] = {}
    for node_id, candidate_matches in matches_by_node.items():
        if not candidate_matches:
            continue
        best = max(
            candidate_matches,
            key=lambda m: (m.score, m.candidate.canonical_key),
        )
        per_node_best[node_id] = best

    # Apply in descending score order (greedy) with a deterministic tie-break;
    # at v1 (merge-allowed) every best survives, but the ordering is fixed so
    # the outcome is replayable byte-for-byte.
    ordered = sorted(
        per_node_best.items(),
        key=lambda kv: (-kv[1].score, kv[0], kv[1].candidate.canonical_key),
    )
    assignment = {node_id: match for node_id, match in ordered}
    return AssignmentOutcome(assignment=assignment, abstained=False)
