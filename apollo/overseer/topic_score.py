"""Deterministic topic-based grading score (LLM never touches the number).

Frozen contract: ``docs/superpowers/specs/2026-07-10-apollo-topic-score-design.md``
section 2 (workspace-level spec, outside this repo).

The served grade today is ``compute_rubric()``'s fixed 60/25/15 axis blend
(``rubric.py``), docked afterwards by ``apply.py::rubric_overall_after_penalty``.
That axis view is a second-hand summary of per-node data the coverage +
misconception ledgers already hold, and the misconception dock is invisible in
the served report. ``compute_topic_score`` replaces the axis view with a
**per-reference-node topic checklist**: each reference node (equation,
condition, simplification, procedure_step — the same four types
``rubric.py::_axis_for`` grades) is one topic, weighted by its structural
centrality in the reference graph, credited by ``compute_coverage``'s verdict
for that node, and docked by any misconception finding that localized to it.

Pure module: no IO, no LLM, no DB, no flags. Every input is already-computed
data (``compute_coverage`` output, reference nodes, ``compute_centrality``
output, a merged detection outcome); every output is a frozen dataclass.
Immutable throughout — no argument is ever mutated, only new objects are
returned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from apollo.ontology import Node
from apollo.overseer.misconception_detector.config import CENTRALITY_W_MIN, SEVERITY_CLAMP
from apollo.overseer.misconception_detector.types import ConceptFinding, MergeOutcome
from apollo.overseer.rubric import score_to_letter

# The four reference-node types ``compute_coverage`` actually grades (mirrors
# ``rubric.py::_axis_for`` — ``definition``/``variable_mapping`` feed neither
# the axis rubric nor the topic score; they aren't in ``per_step`` either).
_GRADED_NODE_TYPES = frozenset({"equation", "condition", "simplification", "procedure_step"})

# Synthetic bucket for a misconception finding that cannot be localized to any
# reference node (concept_key not found among the graded topics).
_GENERAL_KEY = "_general"

TopicStatus = Literal["covered", "partial", "missing"]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _finite_score(raw: Any) -> float:
    """Coerce a score value to a finite float in [0, 1]; NaN/inf/non-numeric
    become 0.0. Mirrors ``rubric.py::_finite_score`` — Python's ``min``/``max``
    silently let a NaN through unclamped (``min(1.0, nan) == 1.0``), so an
    explicit ``math.isfinite`` guard is required, not just ``_clamp01``."""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return _clamp01(f)


@dataclass(frozen=True)
class TopicMisconception:
    """One misconception finding attached to a topic (or the `_general` bucket)."""

    canonical_key: str
    resolved: bool
    dock_points: float
    evidence_span: str | None


@dataclass(frozen=True)
class TopicCredit:
    """One reference-node topic's credit, weight, and attached misconceptions."""

    canonical_key: str
    display_name: str | None
    credit: float
    status: TopicStatus
    weight: float
    misconceptions: tuple[TopicMisconception, ...]


@dataclass(frozen=True)
class TopicScoreResult:
    """The full deterministic topic-score result."""

    score: int
    letter: str
    coverage_component: float
    misconception_dock: float
    topics: tuple[TopicCredit, ...]


def _display_name_for(node: Node) -> str | None:
    """Best-effort human label for a reference node, for report rendering.

    Never raises: ``content`` is a typed pydantic payload per node type, but a
    future/unknown shape degrades to ``None`` rather than crashing the score.
    """
    content: Any = node.content
    for field in ("label", "action", "concept", "applies_when"):
        value = getattr(content, field, None)
        if isinstance(value, str) and value:
            return value
    return None


def _topic_status(credit: float, *, in_per_step: bool) -> TopicStatus:
    """Status for the not-fully-covered path (the "covered" status is decided
    inline in ``_credit_for_node`` before this helper is ever reached)."""
    if in_per_step and credit > 0.0:
        return "partial"
    return "missing"


def _credit_for_node(node_id: str, coverage: dict) -> tuple[float, TopicStatus]:
    """Per-topic credit ``c_i`` in [0, 1] + status, per the spec's rule:

    - ``1.0`` if ``per_step[node_id] == "covered"``.
    - else the node's ``procedure_scores[node_id]`` value when present
      (partial credit) — status "partial" iff ``0 < credit < 1``.
    - else ``0.0`` ("missing").
    """
    per_step = coverage.get("per_step", {}) or {}
    procedure_scores = coverage.get("procedure_scores", {}) or {}

    covered = per_step.get(node_id) == "covered"
    if covered:
        return 1.0, "covered"

    if node_id in procedure_scores:
        credit = _finite_score(procedure_scores[node_id])
        in_per_step = node_id in per_step
        return credit, _topic_status(credit, in_per_step=in_per_step)

    return 0.0, "missing"


def _weights_for(node_ids: list[str], centrality: dict[str, float]) -> dict[str, float]:
    """Centrality per node, floored at CENTRALITY_W_MIN, normalized to sum 1.

    Empty ``node_ids`` -> ``{}``. A node absent from ``centrality`` floors at
    CENTRALITY_W_MIN just like the misconception detector's own severity
    weighting (``merge.py::_severity_for``).
    """
    if not node_ids:
        return {}
    floored = {
        nid: max(CENTRALITY_W_MIN, centrality.get(nid, CENTRALITY_W_MIN)) for nid in node_ids
    }
    total = sum(floored.values())
    if total <= 0.0:  # pragma: no cover - defensive
        # CENTRALITY_W_MIN > 0 in every real configuration, so this only
        # guards a pathological override; distribute uniformly rather than
        # divide by zero.
        even = 1.0 / len(node_ids)
        return {nid: even for nid in node_ids}
    return {nid: v / total for nid, v in floored.items()}


def _dedup_findings_by_canonical_key(
    findings: tuple[ConceptFinding, ...],
) -> tuple[ConceptFinding, ...]:
    """Defensive dedup by ``canonical_key`` (== ``signature``), max confidence.

    ``merge_detections`` is expected to dedup upstream (see the design spec's
    "dedup is also a live bug fix" section), but this module dedups again on
    its own inputs so it stays correct even if that upstream fix lands with a
    different shape, races this change, or is bypassed by a caller that feeds
    raw ``ledger_findings`` directly. Keeps the max-confidence instance per
    key; stable order (first-seen position of the kept instance's key).
    """
    best_by_key: dict[str, ConceptFinding] = {}
    order: list[str] = []
    for finding in findings:
        key = finding.signature
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = finding
            order.append(key)
        elif finding.confidence > current.confidence:
            best_by_key[key] = finding
    return tuple(best_by_key[key] for key in order)


def _finding_resolved(finding: ConceptFinding) -> bool:
    """Whether a finding carries a resolution signal (docks zero).

    v1: ``ConceptFinding`` has no resolution field yet — detector-only
    findings are unresolved per the design spec ("no new resolution
    heuristic is built" in this module). This helper is the single seam a
    future clarification-loop resolution signal would flow through.
    """
    return False


def _finding_penalty_share(finding: ConceptFinding, centrality: dict[str, float]) -> float:
    """Centrality-weighted penalty share for one finding, mirroring
    ``merge.py::_severity_for`` (``severity = centrality(concept) * confidence``,
    floored at CENTRALITY_W_MIN for a concept absent from the map)."""
    weight = centrality.get(finding.concept_key, CENTRALITY_W_MIN)
    return weight * finding.confidence


def _topic_key_for_finding(finding: ConceptFinding, topic_keys: frozenset[str]) -> str:
    """Map a finding's ``concept_key`` (== the reference node's ``node_id``,
    per ``detector.py``/``centrality.py``/``gate.py`` convention) to the topic
    it localized to, or the synthetic ``_general`` bucket when unmappable."""
    if finding.concept_key in topic_keys:
        return finding.concept_key
    return _GENERAL_KEY


def compute_topic_score(
    *,
    coverage: dict,
    reference_nodes: list[Node],
    centrality: dict[str, float],
    detection_outcome: MergeOutcome | None,
) -> TopicScoreResult:
    """Deterministic topic-based score: coverage credit minus misconception dock.

    Args:
        coverage: ``compute_coverage`` output (``per_step``/``procedure_scores``).
        reference_nodes: the reference graph's nodes (typically
            ``reference_graph.nodes``); only the four graded types
            (equation/condition/simplification/procedure_step) become topics.
        centrality: ``compute_centrality`` output (``node_id -> weight``).
        detection_outcome: the merged misconception outcome, or ``None`` (no
            detector run / detector produced nothing) -> zero dock.

    Returns:
        A frozen ``TopicScoreResult``. An empty reference graph (no graded
        topics) returns ``score=0``, ``letter="F"``, both components ``0.0``,
        and an empty ``topics`` tuple — never raises.
    """
    graded_nodes = [n for n in reference_nodes if n.node_type in _GRADED_NODE_TYPES]
    topic_keys = frozenset(n.node_id for n in graded_nodes)

    if not graded_nodes:
        return TopicScoreResult(
            score=0,
            letter=score_to_letter(0),
            coverage_component=0.0,
            misconception_dock=0.0,
            topics=(),
        )

    weights = _weights_for([n.node_id for n in graded_nodes], centrality)

    credits: dict[str, float] = {}
    statuses: dict[str, TopicStatus] = {}
    for node in graded_nodes:
        credit, status = _credit_for_node(node.node_id, coverage)
        credits[node.node_id] = credit
        statuses[node.node_id] = status

    coverage_component = sum(weights[nid] * credits[nid] for nid in topic_keys)

    # --- Misconception dock -------------------------------------------------
    findings = tuple(detection_outcome.ledger_findings) if detection_outcome is not None else ()
    deduped = _dedup_findings_by_canonical_key(findings)

    misconceptions_by_topic: dict[str, list[TopicMisconception]] = {nid: [] for nid in topic_keys}
    misconceptions_by_topic[_GENERAL_KEY] = []

    resolved_flags = tuple(_finding_resolved(f) for f in deduped)
    raw_shares = tuple(
        0.0 if resolved else _finding_penalty_share(finding, centrality)
        for finding, resolved in zip(deduped, resolved_flags, strict=True)
    )
    total_dock = sum(raw_shares)
    misconception_dock = min(SEVERITY_CLAMP, total_dock)
    # When the clamp binds, scale each finding's displayed dock_points
    # proportionally so the per-misconception "−N pts" lines shown to the
    # student sum exactly to the dock actually subtracted from the score —
    # unscaled shares would over-claim relative to the clamped total.
    scale = misconception_dock / total_dock if total_dock > SEVERITY_CLAMP else 1.0

    for finding, resolved, raw_share in zip(deduped, resolved_flags, raw_shares, strict=True):
        topic_key = _topic_key_for_finding(finding, topic_keys)
        misconceptions_by_topic.setdefault(topic_key, []).append(
            TopicMisconception(
                canonical_key=finding.signature,
                resolved=resolved,
                dock_points=raw_share * scale,
                evidence_span=finding.evidence_span or None,
            )
        )

    topics: list[TopicCredit] = []
    for node in graded_nodes:
        nid = node.node_id
        topics.append(
            TopicCredit(
                canonical_key=nid,
                display_name=_display_name_for(node),
                credit=credits[nid],
                status=statuses[nid],
                weight=weights[nid],
                misconceptions=tuple(misconceptions_by_topic.get(nid, ())),
            )
        )

    general_misconceptions = misconceptions_by_topic.get(_GENERAL_KEY, [])
    if general_misconceptions:
        topics.append(
            TopicCredit(
                canonical_key=_GENERAL_KEY,
                display_name=None,
                credit=0.0,
                status="missing",
                weight=0.0,
                misconceptions=tuple(general_misconceptions),
            )
        )

    score = int(round(_clamp01(coverage_component - misconception_dock) * 100))
    letter = score_to_letter(score)

    return TopicScoreResult(
        score=score,
        letter=letter,
        coverage_component=coverage_component,
        misconception_dock=misconception_dock,
        topics=tuple(topics),
    )


__all__ = [
    "TopicCredit",
    "TopicMisconception",
    "TopicScoreResult",
    "compute_topic_score",
]
