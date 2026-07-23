"""Pure coverage-based topic scoring for Apollo Done responses.

The retired misconception detector no longer contributes findings or docks.
The topic payload shape is retained, with an empty ``misconceptions`` tuple on
every topic, so existing UI clients continue to deserialize responses safely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from apollo.ontology import EdgeType, KGGraph, Node
from apollo.overseer.rubric import score_to_letter

CENTRALITY_W_MIN = 0.30
_GRADED_NODE_TYPES = frozenset(
    {"equation", "condition", "simplification", "procedure_step"}
)

TopicStatus = Literal["covered", "partial", "missing"]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _finite_score(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return _clamp01(value) if math.isfinite(value) else 0.0


@dataclass(frozen=True)
class TopicMisconception:
    canonical_key: str
    resolved: bool
    dock_points: float
    evidence_span: str | None


@dataclass(frozen=True)
class TopicCredit:
    canonical_key: str
    display_name: str | None
    credit: float
    status: TopicStatus
    weight: float
    misconceptions: tuple[TopicMisconception, ...]
    evidence_span: str | None = None


@dataclass(frozen=True)
class TopicScoreResult:
    score: int
    letter: str
    coverage_component: float
    misconception_dock: float
    topics: tuple[TopicCredit, ...]


def _display_name_for(node: Node) -> str | None:
    content: Any = node.content
    for field in ("label", "action", "concept", "applies_when"):
        value = getattr(content, field, None)
        if isinstance(value, str) and value:
            return value
    return None


def _credit_for_node(node_id: str, coverage: dict) -> tuple[float, TopicStatus]:
    per_step = coverage.get("per_step", {}) or {}
    procedure_scores = coverage.get("procedure_scores", {}) or {}
    covered = per_step.get(node_id) == "covered"
    if node_id in procedure_scores:
        credit = _finite_score(procedure_scores[node_id])
        if covered:
            return credit, "covered"
        return credit, "partial" if node_id in per_step and credit > 0.0 else "missing"
    return (1.0, "covered") if covered else (0.0, "missing")


def _rescale(raw_scores: dict[str, float], node_ids: list[str]) -> dict[str, float]:
    span = 1.0 - CENTRALITY_W_MIN
    return {node_id: CENTRALITY_W_MIN + raw_scores.get(node_id, 0.0) * span for node_id in node_ids}


def compute_centrality(reference_graph: KGGraph) -> dict[str, float]:
    """Compute cycle-safe structural weights for coverage topic scoring."""
    node_ids = [node.node_id for node in reference_graph.nodes]
    if not node_ids:
        return {}
    if len(node_ids) == 1:
        return {node_ids[0]: 1.0}

    out_degree = {node_id: 0 for node_id in node_ids}
    precedes_edges = []
    for edge in reference_graph.edges:
        if edge.edge_type == EdgeType.DEPENDS_ON:
            if edge.from_node_id in out_degree and edge.to_node_id in out_degree:
                out_degree[edge.from_node_id] += 1
        elif edge.edge_type == EdgeType.PRECEDES:
            precedes_edges.append(edge)

    max_degree = max(out_degree.values(), default=0)
    depends = {
        node_id: degree / max_degree if max_degree else 0.0
        for node_id, degree in out_degree.items()
    }
    positions = {node_id: 0.0 for node_id in node_ids}
    if precedes_edges:
        try:
            touched = {edge.from_node_id for edge in precedes_edges} | {
                edge.to_node_id for edge in precedes_edges
            }
            ordered_ids = [
                node.node_id
                for node in reference_graph.topological_order(EdgeType.PRECEDES)
                if node.node_id in touched
            ]
            if len(ordered_ids) > 1:
                last = len(ordered_ids) - 1
                positions.update(
                    {node_id: 1.0 - position / last for position, node_id in enumerate(ordered_ids)}
                )
        except ValueError:
            pass

    combined = {
        node_id: min(1.0, depends[node_id] + positions[node_id])
        for node_id in node_ids
    }
    return _rescale(combined, node_ids)


def _weights_for(node_ids: list[str], centrality: dict[str, float]) -> dict[str, float]:
    if not node_ids:
        return {}
    floored = {
        node_id: max(CENTRALITY_W_MIN, centrality.get(node_id, CENTRALITY_W_MIN))
        for node_id in node_ids
    }
    total = sum(floored.values())
    return {node_id: value / total for node_id, value in floored.items()}


def compute_topic_score(
    *,
    coverage: dict,
    reference_nodes: list[Node],
    centrality: dict[str, float],
    evidence_spans: dict[str, str] | None = None,
) -> TopicScoreResult:
    """Compute topic credit; the retired detector contributes no dock."""
    graded_nodes = [node for node in reference_nodes if node.node_type in _GRADED_NODE_TYPES]
    if not graded_nodes:
        return TopicScoreResult(0, score_to_letter(0), 0.0, 0.0, ())

    weights = _weights_for([node.node_id for node in graded_nodes], centrality)
    topics: list[TopicCredit] = []
    coverage_component = 0.0
    for node in graded_nodes:
        credit, status = _credit_for_node(node.node_id, coverage)
        coverage_component += weights[node.node_id] * credit
        topics.append(
            TopicCredit(
                canonical_key=node.node_id,
                display_name=_display_name_for(node),
                credit=credit,
                status=status,
                weight=weights[node.node_id],
                misconceptions=(),
                evidence_span=(evidence_spans or {}).get(node.node_id),
            )
        )

    score = int(round(_clamp01(coverage_component) * 100))
    return TopicScoreResult(
        score=score,
        letter=score_to_letter(score),
        coverage_component=coverage_component,
        misconception_dock=0.0,
        topics=tuple(topics),
    )


__all__ = [
    "CENTRALITY_W_MIN",
    "TopicCredit",
    "TopicMisconception",
    "TopicScoreResult",
    "compute_centrality",
    "compute_topic_score",
]
