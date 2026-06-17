"""WU-4A1 — canonical-space graphs (S_norm / R_norm) + their pure builders.

This is the "build half" of the §6 grading core. It turns
``(frozen student KGGraph, ResolutionResult, problem dict)`` into two immutable
canonical graphs:

  * ``S_norm`` (:class:`CanonicalGraph`) — the student's nodes MERGED by their
    resolved key (two student nodes that resolve to the same target collapse
    into ONE :class:`CanonicalNode` carrying both raw node-ids + evidence
    spans), edges normalized to canonical endpoints (an edge with an unresolved
    endpoint is DROPPED from comparison but counted), and the unresolved student
    nodes RETAINED as findings-input (§6.3).
  * ``R_norm`` (:class:`ReferenceGraph`) — one node per reference-solution step
    (keyed on its ``entity_key``), the dependency DAG from each step's
    ``depends_on``, and one :class:`ReferencePathView` per ``declared_paths``
    entry. **Built from the problem's OWN ``reference_solution`` ONLY** — never
    the deduped ``apollo_kg_entities`` payload (Decision 3 regression guard).

It computes NO scores, runs NO simulation, persists nothing, and calls neither
Neo4j nor any LLM (scores/findings are WU-4A2). Mirrors ``apollo/resolution/``:
many small pure modules, frozen dataclasses, tuple (immutable) fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.graph_compare.validator import (
    validate_reference,
)
from apollo.ontology.edges import EdgeProvenance, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import NodeType

# Import (don't duplicate) the resolution-layer entry_type -> NodeType table
# (Risk MEDIUM in the plan: importing the `_`-private name beats duplicating it
# and risking drift; candidates.py is NOT edited).
from apollo.resolution.candidates import _ENTRY_TYPE_TO_NODE_TYPE
from apollo.resolution.result import ResolutionResult
from apollo.resolution.tiers import student_surface_text


@dataclass(frozen=True)
class CanonicalNode:
    """One canonical-space node.

    For ``S_norm``: the merge of all student nodes that resolved to
    ``canonical_key``. For ``R_norm``: one reference step."""

    canonical_key: str  # the resolved / entity key (the comparison identity)
    node_type: NodeType
    source_node_ids: tuple[str, ...]  # raw student node-ids merged here (sorted); for R_norm: the ref step id(s)
    evidence_spans: tuple[str, ...]  # surface text per source node (durable provenance, §7); () for R_norm
    symbolic: str | None = None  # equations only
    method: str | None = None  # resolution method for S_norm nodes (None for R_norm)
    confidence: float | None = None  # method-cap confidence for S_norm nodes (None for R_norm)


@dataclass(frozen=True)
class CanonicalEdge:
    """A normalized edge in canonical space (endpoints are canonical_keys)."""

    edge_type: EdgeType
    from_key: str
    to_key: str
    provenance: EdgeProvenance  # carried from the student edge ('explicit'|'inferred')


@dataclass(frozen=True)
class CanonicalGraph:
    """S_norm. Resolved+merged nodes, normalized edges, retained unresolved nodes."""

    nodes: tuple[CanonicalNode, ...]
    edges: tuple[CanonicalEdge, ...]
    unresolved_nodes: tuple[tuple[str, str], ...]  # (raw node_id, surface_text) — findings-input, NOT scored here
    dropped_edge_count: int  # edges dropped because an endpoint was unresolved


@dataclass(frozen=True)
class ReferencePathView:
    """One declared acceptable solution path, as an ordered tuple of canonical keys."""

    canonical_keys: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceGraph:
    """R_norm. Per-problem reference nodes + dependency DAG + declared-path views."""

    nodes: tuple[CanonicalNode, ...]
    edges: tuple[CanonicalEdge, ...]  # dependency edges from each step's depends_on
    paths: tuple[ReferencePathView, ...]  # one per declared_paths entry (>= 1)


# ---------------------------------------------------------------------------
# R_norm builder
# ---------------------------------------------------------------------------


def build_reference_canonical(problem: dict) -> ReferenceGraph:
    """Build ``R_norm`` from the problem's OWN reference solution (Decision 3).

    Validates the reference graph FIRST (the §6.6 "reference fails validation →
    block grading" gate). Reads only ``reference_solution`` /
    ``depends_on`` / ``declared_paths`` — NEVER the deduped entity payload.

    Raises :class:`ReferenceGraphInvalidError` if the reference graph is invalid.
    """
    validate_reference(problem)  # §6.6 gate — raises ReferenceGraphInvalidError

    steps = problem.get("reference_solution", [])

    # step id -> canonical (entity) key, for depends_on + declared_paths mapping.
    key_for_step: dict[str, str] = {step["id"]: step["entity_key"] for step in steps}

    nodes: list[CanonicalNode] = []
    for step in steps:
        entry_type = step["entry_type"]
        node_type: NodeType = _ENTRY_TYPE_TO_NODE_TYPE[entry_type]
        content = step.get("content", {}) or {}
        symbolic = content.get("symbolic") if node_type == "equation" else None
        nodes.append(
            CanonicalNode(
                canonical_key=step["entity_key"],
                node_type=node_type,
                source_node_ids=(step["id"],),
                evidence_spans=(),  # reference side carries no student surface text
                symbolic=symbolic,
                method=None,
                confidence=None,
            )
        )

    # Dependency edges: (dependency_step → step), mapped to canonical keys. These
    # form the R_norm DAG (generic dependency direction — DEPENDS_ON).
    edges: list[CanonicalEdge] = []
    for step in steps:
        to_key = key_for_step[step["id"]]
        for dep_step_id in step.get("depends_on", []):
            from_key = key_for_step[dep_step_id]
            edges.append(
                CanonicalEdge(
                    edge_type=EdgeType.DEPENDS_ON,
                    from_key=from_key,
                    to_key=to_key,
                    provenance="explicit",
                )
            )

    # Per-path views: each declared path is an ordered list of reference-node
    # ids; map each to its canonical key (validate_reference guarantees every id
    # is a real reference-node id and >= 1 path exists).
    paths: list[ReferencePathView] = []
    for path in problem["declared_paths"]:
        paths.append(
            ReferencePathView(canonical_keys=tuple(key_for_step[nid] for nid in path))
        )

    return ReferenceGraph(nodes=tuple(nodes), edges=tuple(edges), paths=tuple(paths))


# ---------------------------------------------------------------------------
# S_norm builder
# ---------------------------------------------------------------------------


def build_student_canonical(
    student_graph: KGGraph, resolution: ResolutionResult
) -> CanonicalGraph:
    """Build ``S_norm`` from the student graph + an already-computed resolution.

    Merges student nodes by ``resolved_key`` (two nodes resolving to the same
    target collapse into ONE :class:`CanonicalNode` carrying both raw node-ids +
    evidence spans), normalizes edges to canonical endpoints (DROPS an edge with
    an unresolved endpoint, counting it in ``dropped_edge_count``), and RETAINS
    unresolved student nodes as ``unresolved_nodes`` findings-input (§6.3).

    Pure + deterministic: the same inputs always yield an equal CanonicalGraph
    (merged node-ids sorted, nodes ordered by canonical_key).
    """
    node_index = student_graph.node_index()

    # node_id -> resolved key (only for nodes that actually resolved).
    resolved_key_by_node: dict[str, str] = {
        rn.node_id: rn.resolved_key
        for rn in resolution.resolved
        if rn.resolution == "resolved" and rn.resolved_key is not None
    }
    # node_id -> ResolvedNode (for method / confidence on the resolved subset).
    rn_by_node = {rn.node_id: rn for rn in resolution.resolved}

    # Group the resolved student nodes by their resolved key (the merge).
    members_by_key: dict[str, list[str]] = {}
    for node_id, key in resolved_key_by_node.items():
        members_by_key.setdefault(key, []).append(node_id)

    nodes: list[CanonicalNode] = []
    for key in sorted(members_by_key):
        member_ids = sorted(members_by_key[key])
        member_nodes = [node_index[nid] for nid in member_ids if nid in node_index]
        # All merged members share one node type (type-compat guarantees it);
        # take the first member's type.
        node_type: NodeType = member_nodes[0].node_type
        evidence = tuple(student_surface_text(n) for n in member_nodes)
        # Equation symbolic: carry the first member's symbolic surface (equations
        # only); None for non-equations.
        symbolic = (
            student_surface_text(member_nodes[0]) if node_type == "equation" else None
        )
        # Deterministic method/confidence: highest confidence wins, tie-broken on
        # method name (sort by (-confidence, method)).
        method, confidence = _winning_method(member_ids, rn_by_node)
        nodes.append(
            CanonicalNode(
                canonical_key=key,
                node_type=node_type,
                source_node_ids=tuple(member_ids),
                evidence_spans=evidence,
                symbolic=symbolic,
                method=method,
                confidence=confidence,
            )
        )

    # Normalize edges: both endpoints must resolve, else DROP (and count).
    edges: list[CanonicalEdge] = []
    dropped = 0
    for edge in student_graph.edges:
        from_key = resolved_key_by_node.get(edge.from_node_id)
        to_key = resolved_key_by_node.get(edge.to_node_id)
        if from_key is None or to_key is None:
            dropped += 1
            continue
        edges.append(
            CanonicalEdge(
                edge_type=edge.edge_type,
                from_key=from_key,
                to_key=to_key,
                provenance=edge.provenance,
            )
        )

    # Retain unresolved student nodes (findings-input, NOT dropped, NOT scored).
    unresolved: list[tuple[str, str]] = []
    for rn in resolution.resolved:
        if rn.node_id in resolved_key_by_node:
            continue
        node = node_index.get(rn.node_id)
        surface = student_surface_text(node) if node is not None else ""
        unresolved.append((rn.node_id, surface))
    unresolved.sort()

    return CanonicalGraph(
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved_nodes=tuple(unresolved),
        dropped_edge_count=dropped,
    )


def _winning_method(
    member_ids: list[str], rn_by_node: dict
) -> tuple[str | None, float | None]:
    """Pick the merged node's (method, confidence) deterministically: the
    highest-confidence member wins, tie-broken on method name (sort by
    ``(-confidence, method)``). Returns ``(None, None)`` when no member carries a
    ResolvedNode (defensive — every merged member is resolved)."""
    candidates = [
        (rn_by_node[nid].confidence, rn_by_node[nid].method)
        for nid in member_ids
        if nid in rn_by_node
    ]
    if not candidates:  # pragma: no cover - defensive: merged members are resolved
        return (None, None)
    candidates.sort(key=lambda cm: (-cm[0], cm[1]))
    confidence, method = candidates[0]
    return (method, confidence)
