"""WU-4A2 — the in-memory finding vocabulary (§2 finding_kind set).

A :class:`Finding` is the diagnostic unit the score passes emit. It is a pure
in-memory dataclass that maps 1:1 onto the ``apollo_graph_comparison_findings``
columns so WU-4B can persist it with no reshaping — but THIS unit does NO
persistence and NO finding->event conversion (that decision table is §6.5,
WU-4B). A finding therefore carries **no event field at all**: sub-scores never
produce events (§6.2 edge demotion + §6.5 seam).

:class:`FindingKind` mirrors :data:`apollo.persistence.models.FINDING_KINDS`
1:1 (a test asserts the value-set equality so the two can never drift). It is a
``StrEnum`` (like :class:`apollo.ontology.edges.EdgeType`) so ``finding.kind ==
"covered_node"`` works against WU-4B's plain-string persistence column.

Every reducer helper is PURE: it reads a frozen input and returns a NEW
:class:`Finding`, never mutating anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from apollo.graph_compare.canonical import CanonicalEdge, CanonicalNode


class FindingKind(StrEnum):
    """The §2 finding-kind set. Value-set == ``models.FINDING_KINDS`` (asserted)."""

    COVERED_NODE = "covered_node"
    MISSING_NODE = "missing_node"
    MATCHED_EDGE = "matched_edge"
    MISSING_EDGE = "missing_edge"
    UNSUPPORTED_EXTRA = "unsupported_extra"
    CONTRADICTION = "contradiction"
    UNRESOLVED = "unresolved"
    ALTERNATIVE_PATH = "alternative_path"
    COVERED_BY_CONTRACTION = "covered_by_contraction"
    NOT_DEMONSTRATED = "not_demonstrated"


@dataclass(frozen=True)
class Finding:
    """One diagnostic finding. Fields map onto ``apollo_graph_comparison_findings``.

    ``student_edge_ids`` / ``reference_edge_ids`` columns exist on the table but
    edges here are diagnostic-only and keyed by ``from_key -> to_key`` text
    (carried in ``message``), so this in-memory shape omits them and WU-4B
    defaults them to ``[]``. There is intentionally NO ``event``/``event_kind``
    field — that conversion is WU-4B (§6.5)."""

    kind: FindingKind
    canonical_key: str | None = None
    student_node_ids: tuple[str, ...] = ()
    reference_node_ids: tuple[str, ...] = ()
    evidence_spans: tuple[str, ...] = ()
    score: float | None = None
    confidence: float | None = None
    message: str | None = None


def covered_finding(node: CanonicalNode) -> Finding:
    """A reference key the student covered. Carries the S_norm node's evidence,
    confidence (already method-capped upstream), and merged source node-ids."""
    return Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key=node.canonical_key,
        student_node_ids=node.source_node_ids,
        evidence_spans=node.evidence_spans,
        confidence=node.confidence,
    )


def missing_finding(ref_node: CanonicalNode) -> Finding:
    """A reference key the student did NOT cover (on the winning path). ``score``
    0.0; NO event decision (that is WU-4B)."""
    return Finding(
        kind=FindingKind.MISSING_NODE,
        canonical_key=ref_node.canonical_key,
        reference_node_ids=ref_node.source_node_ids,
        score=0.0,
    )


def contraction_finding(
    ref_node: CanonicalNode,
    *,
    kind: FindingKind,
    student_node_ids: tuple[str, ...],
    evidence_spans: tuple[str, ...],
    predecessor_key: str,
    successor_key: str,
    bridge_provenance: tuple[str, ...],
    entailment: float | None,
    decision_channel: str,
) -> Finding:
    """An eligible bridge's safeguarded contraction outcome with provenance."""
    return Finding(
        kind=kind,
        canonical_key=ref_node.canonical_key,
        student_node_ids=student_node_ids,
        reference_node_ids=ref_node.source_node_ids,
        evidence_spans=evidence_spans,
        score=entailment,
        message=(
            f"chain {predecessor_key} -> {ref_node.canonical_key} -> {successor_key}; "
            f"bridge {', '.join(bridge_provenance) or 'merged-student-node'}; "
            f"decision {decision_channel}"
        ),
    )


def contradiction_finding(node: CanonicalNode) -> Finding:
    """An S_norm node whose key is a misconception (a resolved contradiction;
    §6.2). ``score`` 0.0; misconception key + evidence carried."""
    return Finding(
        kind=FindingKind.CONTRADICTION,
        canonical_key=node.canonical_key,
        student_node_ids=node.source_node_ids,
        evidence_spans=node.evidence_spans,
        score=0.0,
    )


def unsupported_extra_finding(node: CanonicalNode) -> Finding:
    """An S_norm node matching no reference key and not a misconception (an
    honest non-detection, NOT a contradiction; §6.11). Diagnostic only — NO
    penalty marker (``score`` stays None)."""
    return Finding(
        kind=FindingKind.UNSUPPORTED_EXTRA,
        canonical_key=node.canonical_key,
        student_node_ids=node.source_node_ids,
        evidence_spans=node.evidence_spans,
    )


def unresolved_finding(node_id: str, surface: str) -> Finding:
    """An unresolved student node carried from ``CanonicalGraph.unresolved_nodes``
    (§6.3). ZERO soundness penalty; surfaced for the diagnostic transcript."""
    return Finding(
        kind=FindingKind.UNRESOLVED,
        student_node_ids=(node_id,),
        evidence_spans=(surface,),
    )


def matched_edge_finding(edge: CanonicalEdge) -> Finding:
    """A reference edge matched by an S_norm edge (diagnostic). The edge is keyed
    in ``message`` as ``"<from> -<TYPE>-> <to> (<provenance>)"``; edge-id tuples
    stay empty (edges are diagnostic-only, never scored as node ids)."""
    return Finding(kind=FindingKind.MATCHED_EDGE, message=_edge_message(edge))


def missing_edge_finding(edge: CanonicalEdge) -> Finding:
    """A reference edge with no S_norm match (diagnostic only; never an event)."""
    return Finding(kind=FindingKind.MISSING_EDGE, message=_edge_message(edge))


def alternative_path_finding(path_index: int, canonical_keys: tuple[str, ...]) -> Finding:
    """The student took a declared alternative path (winning path != path 0).
    Emitted so the multi-path schema is exercised from day one."""
    return Finding(
        kind=FindingKind.ALTERNATIVE_PATH,
        reference_node_ids=canonical_keys,
        message=f"student took declared alternative path {path_index}",
    )


def _edge_message(edge: CanonicalEdge) -> str:
    return f"{edge.from_key} -{edge.edge_type}-> {edge.to_key} ({edge.provenance})"
