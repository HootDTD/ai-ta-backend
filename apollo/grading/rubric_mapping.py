"""WU-4C2 §6.4 — map graph-sim findings + reference graph onto the FROZEN
``compute_rubric`` input bag (so the graph-sim candidate grade mirrors the OLD
student-facing rubric's call shape, with graph-sim-derived inputs).

PURE: reads the frozen ``AuditedGrade`` + ``ReferenceGraph`` (+ the injected
``opposes_map`` / ``turn_order``) and returns NEW dicts/tuples — never mutates
its inputs, never calls an LLM, never touches Postgres/Neo4j.

The reference identity in graph-sim space is ``CanonicalNode.canonical_key``
(R_norm nodes carry NO ``node_id``). We KEY the coverage dict on ``canonical_key``
and build a :class:`RubricRefNode` (``node_id=canonical_key``, ``node_type``) per
reference node, so ``compute_rubric``'s ``r.node_id``/``r.node_type`` duck-type
lookups line up WITHOUT importing the heavy ``apollo.ontology.Node`` union
(``compute_rubric`` reads ONLY those two attributes — verified rubric.py:112-133).

The ``misconception_scores`` (``{bank_code -> 0.5 | 1.0}``) mirror the rubric's
existing P2.8 contract:
  * a CONTRADICTION whose ``opposes_map`` target has a COVERED finding that came
    LATER (turn_order) than the contradiction = RESOLVED -> ``1.0``.
  * any other detected CONTRADICTION = detected-unresolved -> ``0.5``.
  * never-detected misconceptions do not appear (the rubric treats the axis as
    absent).

CAVEAT (RECON #7): ``opposes_map`` is structurally EMPTY today, so in practice
every detected misconception maps to ``0.5``. The ``1.0`` branch still exists and
is unit-tested with a SYNTHETIC ``opposes_map`` fixture so flipping opposes later
needs no rubric change.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.canonical import ReferenceGraph
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.overseer.rubric import compute_rubric

# Turn position for a finding whose evidence node ids are all absent from the
# injected turn_order — order unknown. Copies the events.py ``_turn_position``
# SHAPE (min over student_node_ids, absent -> +inf sentinel) WITHOUT importing
# the event decision table (this is the rubric AXIS, decoupled from §6.5 events).
_SENTINEL_TURN: float = math.inf

# A covered procedure key with no carried confidence scores a full 1.0 (the
# audit/resolution already vouched for the coverage).
_DEFAULT_COVERED_PROCEDURE_SCORE: float = 1.0

# Detected-unresolved vs detected-resolved misconception axis scores (the P2.8
# rubric contract: 0.5 detected-unresolved, 1.0 detected-and-resolved).
_MISCONCEPTION_UNRESOLVED_SCORE: float = 0.5
_MISCONCEPTION_RESOLVED_SCORE: float = 1.0


@dataclass(frozen=True)
class RubricRefNode:
    """The duck-typed reference-node adapter ``compute_rubric`` needs: it reads
    ONLY ``.node_id`` + ``.node_type`` off a reference node. ``node_id`` carries
    the graph-sim ``canonical_key`` (R_norm nodes have no ``node_id``)."""

    node_id: str
    node_type: str  # 'procedure_step'|'condition'|'simplification'|'equation'|'definition'|'variable_mapping'


@dataclass(frozen=True)
class RubricMappingInput:
    """The exact bag ``compute_rubric(coverage, reference_nodes, *,
    misconception_scores=...)`` consumes, derived from graph-sim findings."""

    coverage: dict
    reference_nodes: tuple[RubricRefNode, ...]
    misconception_scores: dict[str, float]


def _turn_position(finding: Finding, turn_order: Mapping[str, int]) -> float:
    """The finding's turn position = ``min`` over its ``student_node_ids`` (anchor
    to the EARLIEST assertion). An absent id contributes the ``+inf`` sentinel;
    with no resolvable id the whole finding is the sentinel."""
    positions = [turn_order.get(nid, _SENTINEL_TURN) for nid in finding.student_node_ids]
    if not positions:
        return _SENTINEL_TURN
    return min(positions)


def _bucket_findings(
    findings: tuple[Finding, ...],
) -> tuple[dict[str, Finding], set[str], set[str], list[Finding]]:
    """Partition findings into covered, missing, neutral, and contradiction bags.

    Covered-by-contraction shares the covered bag; not-demonstrated stays
    distinct so the rubric input can expose the neutral status."""
    covered: dict[str, Finding] = {}
    missing: set[str] = set()
    not_demonstrated: set[str] = set()
    contradictions: list[Finding] = []
    for finding in findings:
        key = finding.canonical_key
        if finding.kind == FindingKind.CONTRADICTION:
            contradictions.append(finding)
            continue
        if key is None:
            continue
        if finding.kind in (
            FindingKind.COVERED_NODE,
            FindingKind.COVERED_BY_CONTRACTION,
        ):
            covered[key] = finding
        elif finding.kind == FindingKind.MISSING_NODE:
            missing.add(key)
        elif finding.kind == FindingKind.NOT_DEMONSTRATED:
            not_demonstrated.add(key)
    return covered, missing, not_demonstrated, contradictions


def _coverage_for_node(
    canonical_key: str, node_type: str, covered: dict[str, Finding]
) -> tuple[str, float | None]:
    """The (per_step status, procedure_score|None) for one reference node.

    A covered key -> ``"covered"`` (+ the finding confidence, or the default for
    a covered procedure with no confidence). Any other key -> ``"missing"`` (+
    ``0.0`` procedure score). procedure_score is None for non-procedure nodes."""
    is_procedure = node_type == "procedure_step"
    if canonical_key in covered:
        finding = covered[canonical_key]
        if not is_procedure:
            return "covered", None
        score = (
            finding.confidence
            if finding.confidence is not None
            else _DEFAULT_COVERED_PROCEDURE_SCORE
        )
        return "covered", score
    # missing-or-unknown reference key (conservative default).
    return "missing", (0.0 if is_procedure else None)


def _misconception_scores(
    covered: dict[str, Finding],
    contradictions: list[Finding],
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> dict[str, float]:
    """Per detected misconception key -> 0.5 (detected-unresolved) or 1.0
    (detected-and-resolved). Resolved iff the opposed entity has a COVERED
    finding that came LATER (turn_order) than the contradiction."""
    scores: dict[str, float] = {}
    for contradiction in contradictions:
        key = contradiction.canonical_key
        if key is None:
            continue
        scores[key] = _resolution_state(contradiction, covered, opposes_map, turn_order)
    return scores


def _resolution_state(
    contradiction: Finding,
    covered: dict[str, Finding],
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> float:
    """1.0 iff the misconception's opposed entity has a COVERED finding asserted
    LATER than the contradiction; else 0.5 (detected-unresolved)."""
    key = contradiction.canonical_key
    opposed_key = opposes_map.get(key) if key is not None else None
    if opposed_key is None or opposed_key not in covered:
        return _MISCONCEPTION_UNRESOLVED_SCORE
    covered_finding = covered[opposed_key]
    if _turn_position(covered_finding, turn_order) > _turn_position(contradiction, turn_order):
        return _MISCONCEPTION_RESOLVED_SCORE
    return _MISCONCEPTION_UNRESOLVED_SCORE


def findings_to_rubric_input(
    *,
    audited: AuditedGrade,
    reference_graph: ReferenceGraph,
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> RubricMappingInput:
    """Map ``AuditedGrade.findings`` + the graph-sim ``ReferenceGraph`` into the
    ``compute_rubric`` input bag (keyed on ``canonical_key``). PURE — builds NEW
    dicts/tuples; never mutates the inputs."""
    covered, missing, not_demonstrated, contradictions = _bucket_findings(audited.findings)

    per_step: dict[str, str] = {}
    procedure_scores: dict[str, float] = {}
    reference_nodes: list[RubricRefNode] = []
    for ref in reference_graph.nodes:
        canonical_key = ref.canonical_key
        node_type = ref.node_type
        status, proc_score = _coverage_for_node(canonical_key, node_type, covered)
        if canonical_key in not_demonstrated:
            status = "not_demonstrated"
        per_step[canonical_key] = status
        if proc_score is not None:
            procedure_scores[canonical_key] = proc_score
        reference_nodes.append(RubricRefNode(node_id=canonical_key, node_type=node_type))

    coverage = {"per_step": per_step, "procedure_scores": procedure_scores}
    misconception_scores = _misconception_scores(covered, contradictions, opposes_map, turn_order)

    return RubricMappingInput(
        coverage=coverage,
        reference_nodes=tuple(reference_nodes),
        misconception_scores=misconception_scores,
    )


def build_graph_sim_rubric(
    *,
    audited: AuditedGrade,
    reference_graph: ReferenceGraph,
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> dict:
    """The graph-sim candidate rubric: maps findings onto the ``compute_rubric``
    bag and calls the FROZEN ``compute_rubric`` (NO reimplementation — mirrors the
    OLD path's ``compute_rubric(coverage, reference_nodes,
    misconception_scores=...)`` call shape, only with graph-sim-derived inputs)."""
    mapped = findings_to_rubric_input(
        audited=audited,
        reference_graph=reference_graph,
        opposes_map=opposes_map,
        turn_order=turn_order,
    )
    return compute_rubric(
        mapped.coverage,
        list(mapped.reference_nodes),  # type: ignore[arg-type]  # RubricRefNode is structurally compatible (node_id/node_type)
        misconception_scores=mapped.misconception_scores,
    )
