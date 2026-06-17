"""WU-4A1 — raw-graph grammar validation (the §6.4 step-4 + §6.6 gates).

Two entry points, two named errors (NO-FALLBACK convention: structured reason
fields, raised loudly, never swallowed):

  * :func:`validate_student_graph` — the §6.4 step-4 gate over the RAW student
    KGGraph. Re-derives each edge's endpoint node types from the graph's
    ``node_index()`` (parser edges may carry ``None`` type fields, so the
    optional ``from_node_type`` / ``to_node_type`` on the Edge are NOT trusted),
    then checks EDGE_ALLOWED_PAIRS, endpoint existence, ``attempt_id`` scoping
    uniformity (all nodes + edges share ONE attempt_id), and no PRECEDES cycle
    (via :meth:`KGGraph.topological_order`). Per-node required fields are
    guaranteed by Pydantic at construction, so only cross-node invariants are
    re-checked here. Raises :class:`StudentGraphInvalidError`.
  * :func:`validate_reference` — the §6.6 reference gate. DELEGATES to the WU-3B
    ``validate_reference_graph`` (REUSE — never reimplement §6.1) and re-raises a
    failure as :class:`ReferenceGraphInvalidError` carrying that function's
    ``errors`` verbatim.

Pure + DB-free + LLM-free; mirrors ``apollo/resolution/`` style.
"""

from __future__ import annotations

from apollo.ontology.edges import EDGE_ALLOWED_PAIRS, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.persistence.learner_model_seed import validate_reference_graph


class StudentGraphInvalidError(ValueError):
    """Raised when the RAW student graph violates the §6.4 step-4 grammar
    (illegal edge endpoints, missing endpoints, mixed attempt_id, or a PRECEDES
    cycle). Carries the structured ``reasons`` tuple."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__("; ".join(self.reasons))


class ReferenceGraphInvalidError(ValueError):
    """Raised when the reference graph fails the §6.1 validation contract (the
    §6.6 block-grading gate). ``reasons`` are exactly
    ``validate_reference_graph(...).errors``."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__("; ".join(self.reasons))


def validate_student_graph(student_graph: KGGraph) -> None:
    """Validate the RAW student graph grammar (§6.4 step-4).

    Returns ``None`` on a well-formed graph; raises
    :class:`StudentGraphInvalidError` with every violation collected in
    ``reasons`` otherwise.
    """
    reasons: list[str] = []
    node_index = student_graph.node_index()

    # attempt_id scoping uniformity — all nodes AND all edges share ONE id.
    attempt_ids = {n.attempt_id for n in student_graph.nodes}
    attempt_ids |= {e.attempt_id for e in student_graph.edges}
    if len(attempt_ids) > 1:
        reasons.append(
            f"nodes/edges span multiple attempt_ids: {sorted(attempt_ids)} "
            "(attempt scoping must be uniform)"
        )

    # Edge endpoint existence + EDGE_ALLOWED_PAIRS re-derived from node types.
    for edge in student_graph.edges:
        from_node = node_index.get(edge.from_node_id)
        to_node = node_index.get(edge.to_node_id)
        if from_node is None:
            reasons.append(
                f"edge {edge.edge_type} references missing from_node_id "
                f"{edge.from_node_id!r}"
            )
        if to_node is None:
            reasons.append(
                f"edge {edge.edge_type} references missing to_node_id "
                f"{edge.to_node_id!r}"
            )
        if from_node is None or to_node is None:
            continue
        # Re-derive endpoint types from the graph (do NOT trust the optional
        # from_node_type / to_node_type on the Edge — parser edges leave them
        # None, and a constructed bad pair can bypass the Edge validator).
        pair = (from_node.node_type, to_node.node_type)
        allowed = EDGE_ALLOWED_PAIRS[edge.edge_type]
        if pair not in allowed:
            reasons.append(
                f"edge {edge.edge_type} not allowed for "
                f"({from_node.node_type} -> {to_node.node_type})"
            )

    # No PRECEDES cycle (topological_order raises ValueError on a cycle).
    try:
        student_graph.topological_order(EdgeType.PRECEDES)
    except ValueError as exc:
        reasons.append(f"PRECEDES subgraph has a cycle: {exc}")

    if reasons:
        raise StudentGraphInvalidError(reasons=tuple(reasons))
    return None


def validate_reference(problem: dict) -> None:
    """Validate the reference graph (the §6.6 gate). Delegates to the WU-3B
    ``validate_reference_graph`` (REUSE); on failure raises
    :class:`ReferenceGraphInvalidError` carrying its ``errors`` verbatim."""
    result = validate_reference_graph(problem)
    if not result.ok:
        raise ReferenceGraphInvalidError(reasons=result.errors)
    return None
