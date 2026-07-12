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
    (keyed on its ``entity_key``); its edges are the dependency DAG from each
    step's ``depends_on`` PLUS the USES edges from each procedure step's
    ``uses_equations`` and the PRECEDES chain over procedure ``content.order``
    (edge-symmetric with ``Problem.to_kg_graph`` so the student's real
    USES/PRECEDES edges have reference targets to match); and one
    :class:`ReferencePathView` per ``declared_paths`` entry. **Built from the
    problem's OWN ``reference_solution`` ONLY** — never the deduped
    ``apollo_kg_entities`` payload (Decision 3 regression guard).

It computes NO scores, runs NO simulation, persists nothing, and calls neither
Neo4j nor any LLM (scores/findings are WU-4A2). Mirrors ``apollo/resolution/``:
many small pure modules, frozen dataclasses, tuple (immutable) fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from apollo.graph_compare.validator import (
    validate_reference,
)
from apollo.ontology.edges import EdgeProvenance, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import NodeType

# Import (don't duplicate) the resolution-layer entry_type -> NodeType resolver
# (importing the `_`-private helper beats duplicating the table and risking
# drift). ``_node_type_for_entry`` returns ``None`` for an unmapped entry_type so
# R_norm can DEGRADE that step (G4) instead of KeyError-ing.
from apollo.resolution.candidates import _node_type_for_entry
from apollo.resolution.result import ResolutionResult
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanonicalNode:
    """One canonical-space node.

    For ``S_norm``: the merge of all student nodes that resolved to
    ``canonical_key``. For ``R_norm``: one reference step."""

    canonical_key: str  # the resolved / entity key (the comparison identity)
    node_type: NodeType
    source_node_ids: tuple[
        str, ...
    ]  # raw student node-ids merged here (sorted); for R_norm: the ref step id(s)
    evidence_spans: tuple[
        str, ...
    ]  # surface text per source node (durable provenance, §7); () for R_norm
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
    unresolved_nodes: tuple[
        tuple[str, str], ...
    ]  # (raw node_id, surface_text) — findings-input, NOT scored here
    dropped_edge_count: int  # edges dropped because an endpoint was unresolved


@dataclass(frozen=True)
class ReferencePathView:
    """One declared acceptable solution path, as an ordered tuple of canonical keys."""

    canonical_keys: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceGraph:
    """R_norm. Per-problem reference nodes + dependency DAG + declared-path views."""

    nodes: tuple[CanonicalNode, ...]
    # DEPENDS_ON (depends_on) + USES (uses_equations) + PRECEDES (procedure order)
    edges: tuple[CanonicalEdge, ...]
    paths: tuple[ReferencePathView, ...]  # one per declared_paths entry (>= 1)


# ---------------------------------------------------------------------------
# R_norm builder
# ---------------------------------------------------------------------------


def build_reference_canonical(problem: dict) -> ReferenceGraph:
    """Build ``R_norm`` from the problem's OWN reference solution (Decision 3).

    Validates the reference graph FIRST (the §6.6 "reference fails validation →
    block grading" gate). Reads only ``reference_solution`` (its ``depends_on``,
    procedure ``content.uses_equations`` + ``content.order``) and
    ``declared_paths`` — NEVER the deduped entity payload.

    Raises :class:`ReferenceGraphInvalidError` if the reference graph is invalid.
    """
    validate_reference(problem)  # §6.6 gate — raises ReferenceGraphInvalidError

    steps = problem.get("reference_solution", [])

    # G4 tolerance: DEGRADE steps whose entry_type has no ontology NodeType (a
    # resolver-map/mint-map drift) rather than KeyError. A dropped step is
    # excluded from the node set, from ``key_for_step``, and from every edge /
    # declared-path that references it, so R_norm stays internally consistent.
    # For the recognized types (which now include ``variable_mapping``) this is
    # byte-identical to before: no seeded/authored known-type step is skipped.
    known_steps = [s for s in steps if _node_type_for_entry(s["entry_type"]) is not None]

    # step id -> canonical (entity) key, for depends_on + declared_paths mapping.
    key_for_step: dict[str, str] = {step["id"]: step["entity_key"] for step in known_steps}

    nodes: list[CanonicalNode] = []
    for step in known_steps:
        entry_type = step["entry_type"]
        node_type = _node_type_for_entry(entry_type)
        assert node_type is not None  # known_steps filtered out the unmapped ones
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
    for step in known_steps:
        to_key = key_for_step[step["id"]]
        for dep_step_id in step.get("depends_on", []):
            if dep_step_id not in key_for_step:
                continue  # G4: dependency on a degraded (unmapped) step — drop the edge
            from_key = key_for_step[dep_step_id]
            edges.append(
                CanonicalEdge(
                    edge_type=EdgeType.DEPENDS_ON,
                    from_key=from_key,
                    to_key=to_key,
                    provenance="explicit",
                )
            )

    # USES edges (procedure_step → equation): mirror ``Problem.to_kg_graph`` so
    # the student's real USES edges have a reference target to match. WITHOUT
    # these the ``usage`` dimension is vacuously 1.0 and ``edge_coverage``
    # collapses onto the DEPENDS_ON-only set (the "structural edge scoring is
    # dead by construction" regression). ``uses_equations`` holds equation STEP
    # ids, mapped to canonical keys via ``key_for_step`` (same as depends_on).
    for step in known_steps:
        if step["entry_type"] != "procedure_step":
            continue
        from_key = key_for_step[step["id"]]
        content = step.get("content", {}) or {}
        for eq_step_id in content.get("uses_equations", []) or []:
            if eq_step_id not in key_for_step:
                continue  # G4: USES a degraded (unmapped) equation step — drop the edge
            edges.append(
                CanonicalEdge(
                    edge_type=EdgeType.USES,
                    from_key=from_key,
                    to_key=key_for_step[eq_step_id],
                    provenance="explicit",
                )
            )

    # PRECEDES chain across procedure steps in ``content.order``: mirror
    # ``Problem.to_kg_graph`` (consecutive prev → next). Gives ``edge_coverage``
    # the procedure-order structure the student's PRECEDES edges can match.
    proc_steps = sorted(
        (s for s in known_steps if s["entry_type"] == "procedure_step"),
        key=lambda s: int((s.get("content", {}) or {})["order"]),
    )
    for prev, nxt in zip(proc_steps, proc_steps[1:], strict=False):
        edges.append(
            CanonicalEdge(
                edge_type=EdgeType.PRECEDES,
                from_key=key_for_step[prev["id"]],
                to_key=key_for_step[nxt["id"]],
                provenance="explicit",
            )
        )

    # Per-path views: each declared path is an ordered list of reference-node
    # ids; map each to its canonical key (validate_reference guarantees every id
    # is a real reference-node id and >= 1 path exists). G4: a path node that
    # was degraded (unmapped entry_type) is dropped from the path, keeping the
    # remaining known subsequence rather than KeyError-ing.
    paths: list[ReferencePathView] = []
    for path in problem["declared_paths"]:
        paths.append(
            ReferencePathView(
                canonical_keys=tuple(key_for_step[nid] for nid in path if nid in key_for_step)
            )
        )

    return ReferenceGraph(nodes=tuple(nodes), edges=tuple(edges), paths=tuple(paths))


# ---------------------------------------------------------------------------
# S_norm builder
# ---------------------------------------------------------------------------


def build_student_canonical(student_graph: KGGraph, resolution: ResolutionResult) -> CanonicalGraph:
    """Build ``S_norm`` from the student graph + an already-computed resolution.

    Merges student nodes by ``resolved_key`` (two nodes resolving to the same
    target collapse into ONE :class:`CanonicalNode` carrying both raw node-ids +
    evidence spans), normalizes edges to canonical endpoints (DROPS an edge with
    an unresolved endpoint, counting it in ``dropped_edge_count``), and RETAINS
    unresolved student nodes as ``unresolved_nodes`` findings-input (§6.3).

    The parser-layer DEPENDS_ON contract is ``dependent -> prerequisite``
    ("from relies on to"). Canonical graphs use the opposite, single KG grading
    convention: ``prerequisite -> dependent``. This builder therefore flips
    only DEPENDS_ON endpoints; PRECEDES/USES/SCOPES remain verbatim.

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
        # Explicit, observable merge type. After the resolver type-gate (0.1) all
        # merged members share one type, so the common path is byte-identical
        # (first member's type). A disagreement should NEVER fire — when it does
        # we make the mis-label explicit + deterministic instead of silently
        # taking [0]: lowest node_type (lexicographic — NodeType is a str Literal
        # with no canonical priority), then lowest member id; and we WARN.
        member_types = {n.node_type for n in member_nodes}
        if len(member_types) == 1:
            node_type: NodeType = member_nodes[0].node_type
        else:
            node_type = min(
                (n.node_type for n in member_nodes),
                key=lambda t: (
                    t,
                    min(nid for nid in member_ids if node_index[nid].node_type == t),
                ),
            )
            _LOG.warning(
                "canonical_merge_type_disagreement key=%s types=%s chosen=%s",
                key,
                sorted(member_types),
                node_type,
            )
        evidence = tuple(student_surface_text(n) for n in member_nodes)
        # Equation symbolic: carry the first member's symbolic surface (equations
        # only); None for non-equations.
        symbolic = student_surface_text(member_nodes[0]) if node_type == "equation" else None
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
        if from_key == to_key:  # post-merge self-loop (D3)
            dropped += 1
            continue
        # Parser prompts define DEPENDS_ON as "from relies on to"
        # (dependent -> prerequisite). Canonical comparison is uniformly
        # prerequisite -> dependent, matching reference canonical edges.
        if edge.edge_type == EdgeType.DEPENDS_ON:
            from_key, to_key = to_key, from_key
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


def _winning_method(member_ids: list[str], rn_by_node: dict) -> tuple[str | None, float | None]:
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
