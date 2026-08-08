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
_GRADED_NODE_TYPES = frozenset({"equation", "condition", "simplification", "procedure_step"})

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

# P1.2b keep rule (2026-08-07, review fix). The ledger is a LOSSY record of what
# the student taught — the adjudicator reads the transcript and scores every
# graded rubric item whether or not the questioning loop ever minted a row for
# it (in the pilot ~15% of ledger-less graded nodes were NOT `missing`). So the
# exclusion is asymmetric: a node with no ledger row leaves the denominator ONLY
# when the adjudicator also found no credit for it. At or above this threshold —
# the lowest P1.1 adjudication anchor that means "the student landed this" — the
# node is graded normally, because dropping it would confiscate demonstrated
# work from the numerator AND hide it from the narrative (`graded_topics_only`).
UNPROBED_CREDIT_KEEP_THRESHOLD = 0.6

# P1.2b floor (2026-08-07, review fix). Renormalizing over the engaged subset is
# a safety net for budget-starved sessions, NOT a licence to grade a whole
# problem on one node: a student who explains one of five graded topics and
# immediately clicks Done (or whose auto-done fires before the loop mints
# another ledger row) would otherwise renormalize that single node to weight 1.0
# and score A+, making bailing out early the highest-scoring strategy.
#
# This is a MINIMUM WIDTH, not an on/off gate: below it the denominator is
# widened back only as far as the floor (highest-credit dropped node first), so
# the budget-starved sessions P1.2b exists for still get most of the relief. The
# first build restored the FULL denominator instead, which withheld relief
# precisely from the population the spec named as P1.2b's target.
#
# MEASURED SIZING (2026-08-08 replay of the 135 Week-4 prod artifacts, arms in
# `replay/run-p1/`; supersedes the earlier estimate of "16 of 135, 8 of them
# single-probed", which was a static count over the ledger and never a replay
# result). On the 106 gradable attempts, run against the CURRENT rubric bank,
# P1.2b touches the denominator of exactly 2 and changes the score of exactly 1
# (+7 points). It is very nearly INERT today, and that is a rubric fact, not a
# code fact: most prod problems expose 1-3 graded nodes, and the questioning loop
# mints a ledger row for nearly all of them.
#
# Its value is therefore CONTINGENT on the P1.4 re-authored bank. Replayed
# against the 7 re-authored problems (86 attempts), the exclusion moves 23 rows,
# mean +6.45 and max +44. Read that as a CEILING, not a floor: that replay's
# asked-node ledger stores the OLD node ids, so every re-authored node counts as
# unasked by construction and every sub-threshold node is free to drop. The
# adversarial audit (`replay/run-p1/report.md` §5) bracketed the re-authored
# bank's true median in [73, 86] and could not locate it inside that interval.
# The exclusion is also ONE-DIRECTIONAL — `_denominator` only ever drops nodes
# credited below `UNPROBED_CREDIT_KEEP_THRESHOLD`, and the floor re-admits the
# highest-credit dropped node first, so `new_score >= score_with_every_node` held
# on all 192 replayed rows with zero exceptions. This floor is the only brake on
# that, which is why it is a minimum WIDTH rather than a switch.
MIN_GRADED_DENOMINATOR = 2

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


def _denominator_floor(graded_count: int) -> int:
    """Narrowest denominator P1.2b may serve: at least ``MIN_GRADED_DENOMINATOR``
    AND at least half the adjudicated graded nodes, but never more than there are
    graded nodes (a 1-node rubric must not be blocked by an unreachable floor —
    there the filter is a no-op anyway)."""
    return min(max(MIN_GRADED_DENOMINATOR, math.ceil(graded_count / 2)), graded_count)


def _denominator(
    node_ids: list[str],
    credits: dict[str, float],
    asked_node_ids: frozenset[str] | None,
) -> list[str]:
    """The ordered graded node ids the grade is divided by (P1.2b).

    ``None`` (feature not wired / ledger read failed) keeps every adjudicated
    node, reproducing the pre-fix arithmetic. Otherwise a node is KEPT when the
    ledger engaged with it OR the adjudicator credited it at
    ``UNPROBED_CREDIT_KEEP_THRESHOLD`` or above — the asymmetry that stops the
    safety net confiscating work the tally simply failed to record. If fewer
    nodes survive than ``_denominator_floor`` allows, the dropped nodes are put
    back highest-credit first (reference order breaking ties, so the grade is
    reproducible) until the floor is met: widening is a policy decision of ours,
    so it costs the student as little as it can. Order is always reference order
    — never a set — so weight normalization sums the same floats every run.
    """
    if asked_node_ids is None:
        return list(node_ids)
    kept = {
        node_id
        for node_id in node_ids
        if node_id in asked_node_ids or credits[node_id] >= UNPROBED_CREDIT_KEEP_THRESHOLD
    }
    required = _denominator_floor(len(node_ids))
    if len(kept) < required:
        rank = {node_id: index for index, node_id in enumerate(node_ids)}
        dropped = sorted(
            (node_id for node_id in node_ids if node_id not in kept),
            key=lambda node_id: (-credits[node_id], rank[node_id]),
        )
        _LOG.warning(
            "apollo_topic_score_denominator_widened graded=%d kept=%d required=%d",
            len(node_ids),
            len(kept),
            required,
        )
        kept |= set(dropped[: required - len(kept)])
    return [node_id for node_id in node_ids if node_id in kept]


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

    combined = {node_id: min(1.0, depends[node_id] + positions[node_id]) for node_id in node_ids}
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
    node ids the questioning loop actually ENGAGED with this attempt — it really
    asked about them, or the tally recorded the student teaching them. (The
    caller derives it; ``done.py``'s ``_probed_node_ids`` is the live producer,
    and a bare ``QuestionOpportunity`` row is deliberately not enough.) A graded
    node outside the set that the adjudicator ALSO gave no credit is excluded
    from the denominator (weight 0) and reported with status ``unprobed``: in
    the pilot 31% of graded nodes had no row at all and scored ``missing`` 85%
    of the time, so students were failing on topics nobody raised. The exclusion
    is asymmetric — a node the adjudicator credited is graded normally even with
    no row (see ``UNPROBED_CREDIT_KEEP_THRESHOLD``) — and bounded below by
    ``_denominator_floor`` so a collapsed ledger can never renormalize one node
    into the whole grade.

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

    # P1.2b safety net (2026-08-07): the denominator is the graded nodes this
    # attempt can fairly be answerable for — the ones the question ledger
    # engaged with, plus any the adjudicator credited anyway. The rest stay in
    # ``topics`` (so the artifact and the UI can say "not part of this grade")
    # with weight 0 and status ``unprobed``. `_denominator` owns both halves of
    # the rule (asymmetric keep + floor widening) and its result is an ORDERED
    # list, never a set, so weight normalization sums the same floats in the
    # same order on every run — the grade must be reproducible.
    verdicts = {node.node_id: _credit_for_node(node.node_id, coverage) for node in graded_nodes}
    denominator = _denominator(
        [node.node_id for node in graded_nodes],
        {node_id: credit for node_id, (credit, _status) in verdicts.items()},
        asked_node_ids,
    )
    denominator_ids = frozenset(denominator)

    weights = _weights_for(denominator, centrality)
    # INTERACTION5: per-node Hoot-assist flags, present only when a Hoot aside was
    # graded. Absent → every topic reads False (byte-identical to pre-feature).
    assist_map = coverage.get("hoot_assisted", {}) or {}
    topics: list[TopicCredit] = []
    coverage_component = 0.0
    for node in graded_nodes:
        credit, status = verdicts[node.node_id]
        graded = node.node_id in denominator_ids
        weight = weights[node.node_id] if graded else 0.0
        coverage_component += weight * credit
        topics.append(
            TopicCredit(
                canonical_key=node.node_id,
                display_name=_display_name_for(node),
                credit=credit,
                status=status if graded else "unprobed",
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
    "MIN_GRADED_DENOMINATOR",
    "REFERENCE_TEXT_CREDIT_THRESHOLD",
    "UNPROBED_CREDIT_KEEP_THRESHOLD",
    "TopicCredit",
    "TopicMisconception",
    "TopicScoreResult",
    "compute_centrality",
    "compute_topic_score",
    "graded_topics_only",
    "reference_statement_for",
]
