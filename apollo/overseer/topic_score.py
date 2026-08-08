"""Pure coverage-based topic scoring for Apollo Done responses.

The retired misconception detector no longer contributes findings or docks.
The topic payload shape is retained, with an empty ``misconceptions`` tuple on
every topic, so existing UI clients continue to deserialize responses safely.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Literal

from apollo.ontology import EdgeType, KGGraph, Node
from apollo.overseer.rubric import score_to_letter

_LOG = logging.getLogger(__name__)

CENTRALITY_W_MIN = 0.30
_GRADED_NODE_TYPES = frozenset(
    {"equation", "condition", "simplification", "procedure_step"}
)

# D2 post-grade closure (2026-08-07): a topic that earned LESS than this credit
# exposes its reference statement as ``TopicCredit.reference_text`` so the UI can
# render "what full credit looks like". At or above it the field is None — a
# student who already demonstrated the topic is never shown the answer, and the
# reveal is per-node, never the full worked solution.
REFERENCE_TEXT_CREDIT_THRESHOLD = 0.6

# ...and never in aggregate either. A wholly-failed attempt has EVERY graded
# topic below the threshold, so an unbounded per-topic reveal would return the
# complete graded reference solution — which `restart_problem` (still reachable
# from the REPORT phase, still best-grade-wins) turns into a recitable A+. At
# most this many statements ship per attempt, lowest credit first, which also
# matches the narrative's "name at most two gaps" convention so the two surfaces
# talk about the same nodes.
MAX_REFERENCE_TEXT_REVEALS = 2

# P1.2b floor (2026-08-07). Renormalizing over the probed subset is a safety net
# for budget-starved sessions, NOT a licence to grade a whole problem on one
# node: a student who explains one of five graded topics and immediately clicks
# Done (or whose auto-done fires before the loop mints another ledger row) would
# otherwise renormalize that single node to weight 1.0 and score A+, making
# bailing out early the highest-scoring strategy. Below the floor the FULL
# adjudicated denominator is restored (pre-fix behaviour). Prod sizing (135
# Week-4 attempts): P1.2b changes the denominator on 16 of them, and 8 of those
# 16 are exactly the single-probed-node case this floor blocks.
MIN_PROBED_GRADED_NODES = 2

# Ordered content fields rendered into a node's reference statement. Each node
# content model owns a disjoint subset (equation: label/symbolic, condition:
# label/applies_when, simplification: applies_when/transformation,
# procedure_step: action/purpose), so one ordered pass reads naturally for every
# type without a per-type branch. Non-prose fields (``variables``, ``order``,
# ``uses_equations``) are deliberately excluded.
_REFERENCE_TEXT_FIELDS: tuple[str, ...] = (
    "label",
    "concept",
    "term",
    "action",
    "applies_when",
    "symbolic",
    "meaning",
    "symbol",
    "purpose",
    "transformation",
)

# ``unprobed`` (2026-08-07 P1.2b) is NOT a grade: it marks a graded node the
# questioning loop never raised this attempt, which therefore left the
# denominator entirely.
TopicStatus = Literal["covered", "partial", "missing", "unprobed"]


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
    # INTERACTION5: True iff a Hoot lookup aside explained this topic's content
    # FOR the student (flat cap, no earn-back). Additive and defaulted — sourced
    # from ``coverage["hoot_assisted"]`` in ``compute_topic_score``. An absent map
    # leaves every topic False and the result byte-identical to the pre-feature
    # build. Feedback/narrative surfacing (Agent D) reads this field.
    hoot_assisted: bool = False
    # D2 (2026-08-07): this node's reference statement, populated ONLY when
    # ``credit < REFERENCE_TEXT_CREDIT_THRESHOLD`` — the "what full credit looks
    # like" reveal the student-UI renders per missed topic. None otherwise.
    reference_text: str | None = None


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


def reference_statement_for(node: Any) -> str | None:
    """The node's reference statement, or ``None`` when it carries no prose.

    Rendered from the node's own content only (never the problem's full worked
    solution — D2 caps the reveal at one node). Parts are joined with an em
    dash, e.g. ``"Bernoulli — p + q = c"``, ``"Solve for v — isolate the
    unknown"``.
    """
    content: Any = node.content
    parts = [
        value.strip()
        for field in _REFERENCE_TEXT_FIELDS
        if isinstance(value := getattr(content, field, None), str) and value.strip()
    ]
    return " — ".join(parts) if parts else None


def _probe_floor(graded_count: int) -> int:
    """Minimum probed graded nodes required before P1.2b may shrink the
    denominator: at least ``MIN_PROBED_GRADED_NODES`` AND at least half the
    adjudicated graded nodes, but never more than there are graded nodes (a
    1-node rubric must not be permanently blocked by an unreachable floor —
    there the filter is a no-op anyway)."""
    return min(max(MIN_PROBED_GRADED_NODES, math.ceil(graded_count / 2)), graded_count)


def _reveal_reference_text(
    topics: list[TopicCredit], nodes_by_id: dict[str, Any]
) -> list[TopicCredit]:
    """Attach ``reference_text`` to at most ``MAX_REFERENCE_TEXT_REVEALS`` topics.

    Eligibility is per-topic (``credit < REFERENCE_TEXT_CREDIT_THRESHOLD`` and
    the topic was actually graded — an ``unprobed`` topic is "not part of this
    grade", so revealing its answer is leakage with no diagnostic value). The
    CAP is per-attempt, and it is what keeps D2's "never the full worked
    solution" true when every topic failed. Selection is deterministic: lowest
    credit first, then most central (highest weight), then reference order.
    Nodes that render no prose are skipped WITHOUT consuming a slot — the cap
    counts statements, not candidates. Returns a new list; inputs are never
    mutated. ``nodes_by_id`` is keyed by construction over the SAME node list
    the topics were built from, so the lookup below cannot miss.
    """
    order = {topic.canonical_key: index for index, topic in enumerate(topics)}
    eligible = sorted(
        (
            topic
            for topic in topics
            if topic.status != "unprobed" and topic.credit < REFERENCE_TEXT_CREDIT_THRESHOLD
        ),
        key=lambda topic: (topic.credit, -topic.weight, order[topic.canonical_key]),
    )
    revealed: dict[str, str] = {}
    for topic in eligible:
        if len(revealed) >= MAX_REFERENCE_TEXT_REVEALS:
            break
        statement = reference_statement_for(nodes_by_id[topic.canonical_key])
        if statement:
            revealed[topic.canonical_key] = statement
    return [
        replace(topic, reference_text=revealed[topic.canonical_key])
        if topic.canonical_key in revealed
        else topic
        for topic in topics
    ]


def graded_topics_only(result: TopicScoreResult | None) -> TopicScoreResult | None:
    """The narrative/feedback view of a result: ``unprobed`` topics removed.

    P2.1 consistency (2026-08-07): an ``unprobed`` topic is excluded from the
    grade and labelled "not part of this grade" in ``topics[]``, but it still
    carries credit 0 — so any consumer that enumerates topics as gaps (the
    narrative prompt, the structured topic feedback) would name it as something
    the student missed, contradicting the very same payload. Callers that
    narrate gaps pass this view instead; the SERVED ``topics[]`` and the
    artifact keep the full list. Score, letter and components are the
    already-computed grade, carried over untouched — this never re-scores.
    ``None`` passes through so the caller's soft-fail contract is unchanged.
    """
    if result is None:
        return None
    kept = tuple(topic for topic in result.topics if topic.status != "unprobed")
    if len(kept) == len(result.topics):
        return result
    return replace(result, topics=kept)


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
    asked_node_ids: frozenset[str] | None = None,
) -> TopicScoreResult:
    """Compute topic credit; the retired detector contributes no dock.

    ``asked_node_ids`` (2026-08-07 bimodal-fix P1.2b) is the set of reference
    node ids that have a ``QuestionOpportunity`` row for THIS attempt — i.e. the
    questioning loop either asked about them or recorded a tally update for them
    (a node the student taught spontaneously gets a row too, so this is "the
    tutor engaged with it", not merely "it was asked"). Graded nodes outside the
    set are excluded from the denominator (weight 0) and reported with status
    ``unprobed``: in the pilot 31% of graded nodes had no row at all and scored
    ``missing`` 85% of the time, so students were failing on topics nobody
    raised. The exclusion is gated by ``_probe_floor`` so a collapsed ledger can
    never renormalize one node into the whole grade.

    ``None`` (feature not wired / ledger read failed) reproduces the pre-fix
    GRADE ARITHMETIC exactly — score, letter, per-topic credit/weight/status —
    and so does a set covering every graded node. It is NOT a byte-identical
    payload: ``TopicCredit.reference_text`` (D2) is additive and is populated
    from the credit alone, independently of ``asked_node_ids``.
    """
    graded_nodes = [node for node in reference_nodes if node.node_type in _GRADED_NODE_TYPES]
    if not graded_nodes:
        return TopicScoreResult(0, score_to_letter(0), 0.0, 0.0, ())

    # Abstain-not-zero (2026-08-07 bimodal-fix P0.5): a graded node the
    # adjudicator never returned a verdict for — even after the semantic retry
    # — is OMITTED from the coverage maps by `_to_coverage_verdict`. Such a
    # node must leave the denominator, not score 0: weights renormalize over
    # the adjudicated set only. Membership in per_step marks a node as
    # adjudicated (every adjudicated node gets a per_step entry);
    # procedure_scores is checked too for symmetry with `_credit_for_node`.
    # Historical/graph-lane coverage dicts carry every graded id, so this
    # filter is a no-op for them. All graded nodes omitted is an adjudication
    # failure, not a gradable outcome — raise (the Done path soft-fails to the
    # legacy rubric; the serving lane already 503s before reaching here).
    per_step = coverage.get("per_step", {}) or {}
    procedure_scores = coverage.get("procedure_scores", {}) or {}
    adjudicated = [
        node
        for node in graded_nodes
        if node.node_id in per_step or node.node_id in procedure_scores
    ]
    if not adjudicated:
        raise ValueError("no graded node was adjudicated; refusing to score an empty denominator")
    graded_nodes = adjudicated

    # P1.2b safety net (2026-08-07): the denominator is the PROBED subset — the
    # graded nodes the question ledger actually engaged with this attempt. The
    # rest stay in ``topics`` (so the artifact and the UI can say "not part of
    # this grade") with weight 0 and status ``unprobed``.
    #
    # The shrink only happens ABOVE the floor (`_probe_floor`): too few probed
    # nodes and the full adjudicated denominator is restored, so neither an
    # empty denominator nor a one-node denominator can ever be served.
    # Kept as an ORDERED list (not a set) so weight normalization sums the same
    # floats in the same order on every run — the grade must be reproducible.
    probed_order = [node.node_id for node in graded_nodes]
    if asked_node_ids is not None:
        probed = [node_id for node_id in probed_order if node_id in asked_node_ids]
        required = _probe_floor(len(graded_nodes))
        if len(probed) >= required:
            probed_order = probed
        else:
            _LOG.warning(
                "apollo_topic_score_probe_floor_not_met graded=%d probed=%d required=%d",
                len(graded_nodes),
                len(probed),
                required,
            )
    probed_ids = frozenset(probed_order)

    weights = _weights_for(probed_order, centrality)
    # INTERACTION5: per-node Hoot-assist flags, present only when a Hoot aside was
    # graded. Absent → every topic reads False (byte-identical to pre-feature).
    assist_map = coverage.get("hoot_assisted", {}) or {}
    topics: list[TopicCredit] = []
    coverage_component = 0.0
    for node in graded_nodes:
        credit, status = _credit_for_node(node.node_id, coverage)
        probed = node.node_id in probed_ids
        weight = weights[node.node_id] if probed else 0.0
        coverage_component += weight * credit
        topics.append(
            TopicCredit(
                canonical_key=node.node_id,
                display_name=_display_name_for(node),
                credit=credit,
                status=status if probed else "unprobed",
                weight=weight,
                misconceptions=(),
                evidence_span=(evidence_spans or {}).get(node.node_id),
                hoot_assisted=bool(assist_map.get(node.node_id, False)),
            )
        )

    # D2 reveal LAST: eligibility is per-topic but the cap is per-attempt, so it
    # can only be applied once every topic's credit and status is final.
    topics = _reveal_reference_text(topics, {node.node_id: node for node in graded_nodes})

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
    "MAX_REFERENCE_TEXT_REVEALS",
    "MIN_PROBED_GRADED_NODES",
    "REFERENCE_TEXT_CREDIT_THRESHOLD",
    "TopicCredit",
    "TopicMisconception",
    "TopicScoreResult",
    "compute_centrality",
    "compute_topic_score",
    "graded_topics_only",
    "reference_statement_for",
]
