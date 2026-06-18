"""WU-4B2 §6.4 step 16 — the §6.5 finding->event decision table.

The single public callable :func:`convert_findings_to_events` turns an
already-audited, already-abstention-tagged :class:`AuditedGrade` (the frozen
WU-4B1 output) into in-memory :class:`LearnerEvent`s. This unit is PURE: NO DB,
NO LLM, NO Neo4j, NO containers. Persistence of the events (atomic with the
3-state Bayesian belief update) is **WU-5A**; this unit only PRODUCES them.

Turn order is INJECTED (``turn_order: node_id -> turn position``), supplied by
WU-4C from ``apollo_messages.turn_index`` / Neo4j ``created_at`` (by fixtures in
tests). 4B2 NEVER queries Postgres or Neo4j — a :class:`Node` carries no
temporal field, so order CANNOT be read off a finding.

The §6.5 binding rows, in order:

1. Abstention short-circuit (``abstained`` -> ``()``).
2. Group findings by entity (``canonical_key``) into covered/missing/
   contradiction buckets; edge / unsupported_extra / unresolved / matched_edge /
   missing_edge / alternative_path are diagnostic-only (no event).
3. Conflict detection: a CONTRADICTION on ``misc_key`` + a COVERED on
   ``opposes_map[misc_key]`` is an opposed pair. Resolve the pair (consuming
   BOTH keys) before emitting standalone per-entity events.
4. Per opposed pair, decide by turn order (per-finding turn = ``min`` over its
   ``student_node_ids``; an absent id -> ``+inf`` sentinel -> ambiguous):
   contradiction-earlier -> ``corrected``; covered-earlier -> ``misconception``
   (last position wins); equal/both-sentinel -> ``partial`` + mixed flag.
5. Per remaining entity, emit the standalone event (contradiction ->
   misconception; missing+audit-negative -> missing; audit-upgraded covered
   (marker) -> covered <=0.75; plain covered -> covered).
6. Suppression filter (LAST): drop events whose ``event_kind.value`` is in
   ``suppressed_event_kinds`` (``corrected`` survives a ``misconception``
   suppression — distinct value).
7. Deterministic order: sort by ``(canonical_key, event_kind.value)``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from apollo.grading.audited_grade import AUDIT_UPGRADE_MESSAGE, AuditedGrade
from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.graph_compare.findings import Finding, FindingKind

# §6.5 row 2 calibration gate: the covered-with-edge-missing 'partial' variant is
# DISABLED in v1. Enabling it would let an edge gap halve a score = the §6.2
# Layer-3 bias the demotion rule forbids. DO NOT flip without edge-recall proof.
PARTIAL_EDGE_GAP_ENABLED: bool = False

# Ambiguous-order conflict (equal/sentinel turn positions): a low-confidence
# ``partial`` carrying the diagnostic mixed-understanding flag.
AMBIGUOUS_ORDER_CONFIDENCE: float = 0.5
AMBIGUOUS_ORDER_SCORE: float = 0.5
MIXED_UNDERSTANDING_FLAG: str = "mixed-understanding"
EDGE_GAP_FLAG: str = "edge-gap"
DEFAULT_COVERED_SCORE: float = 1.0

# The contradiction finding factory leaves confidence None (only covered sets
# it); WU-4B1 already gated low-confidence misconceptions out, so a surviving
# contradiction is full-confidence for the event row.
_DEFAULT_MISCONCEPTION_CONFIDENCE: float = 1.0

# Turn position for a finding whose evidence node ids are all absent from the
# injected turn_order — order is unknown, so the conflict is ambiguous.
_SENTINEL_TURN: float = math.inf


def convert_findings_to_events(
    audited_grade: AuditedGrade,
    *,
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> tuple[LearnerEvent, ...]:
    """Convert an :class:`AuditedGrade`'s findings into learner-model events per
    §6.5.

    PURE over the injected ``turn_order`` (node_id -> turn position, supplied by
    WU-4C from ``apollo_messages.turn_index``/Neo4j ``created_at``; by fixtures
    in tests). 4B2 NEVER queries ``apollo_messages`` or Neo4j.

    Honors WU-4B1's abstention outcome:
    - ``audited_grade.abstained`` is True -> return ``()`` (no learner update at
      all).
    - otherwise DROP every event whose kind is in
      ``audited_grade.suppressed_event_kinds`` ('missing' drops missing events;
      'misconception' drops misconception events; 'corrected' is NOT
      'misconception' so it survives a misconception suppression).
    """
    if audited_grade.abstained:
        return ()

    contradictions, covered, missing = _bucket_by_kind(audited_grade.findings)

    consumed, conflict_events = _resolve_conflicts(
        contradictions, covered, opposes_map, turn_order
    )

    standalone_events = _emit_standalone(contradictions, covered, missing, consumed)

    events = conflict_events + standalone_events
    events = _apply_suppression(events, audited_grade.suppressed_event_kinds)
    return tuple(sorted(events, key=lambda e: (e.canonical_key, e.event_kind.value)))


def _bucket_by_kind(
    findings: tuple[Finding, ...],
) -> tuple[
    dict[str, Finding],
    dict[str, Finding],
    dict[str, Finding],
]:
    """Partition the event-bearing findings by entity key into
    (contradictions, covered, missing). Diagnostic-only kinds (edge /
    unsupported_extra / unresolved / matched_edge / missing_edge /
    alternative_path) are skipped — they never produce an event."""
    contradictions: dict[str, Finding] = {}
    covered: dict[str, Finding] = {}
    missing: dict[str, Finding] = {}
    for finding in findings:
        key = finding.canonical_key
        if key is None:
            continue
        if finding.kind == FindingKind.CONTRADICTION:
            contradictions[key] = finding
        elif finding.kind == FindingKind.COVERED_NODE:
            covered[key] = finding
        elif finding.kind == FindingKind.MISSING_NODE:
            missing[key] = finding
        # all other kinds: diagnostic-only, no event
    return contradictions, covered, missing


def _is_audit_upgraded(finding: Finding) -> bool:
    """A finding is an audit-upgrade ONLY when it is a covered node carrying the
    ``AUDIT_UPGRADE_MESSAGE`` marker (NOT keyed on confidence — a genuine
    llm-tier covered node also sits at 0.75)."""
    return (
        finding.kind == FindingKind.COVERED_NODE
        and finding.message == AUDIT_UPGRADE_MESSAGE
    )


def _turn_position(finding: Finding, turn_order: Mapping[str, int]) -> float:
    """The finding's turn position = ``min`` over its ``student_node_ids`` (anchor
    to the EARLIEST assertion — a multi-turn restated claim is anchored to when
    the student FIRST asserted it). An absent id contributes the ``+inf``
    sentinel; with no resolvable id the whole finding is the sentinel."""
    positions = [
        turn_order.get(nid, _SENTINEL_TURN) for nid in finding.student_node_ids
    ]
    if not positions:
        return _SENTINEL_TURN
    return min(positions)


def _resolve_conflicts(
    contradictions: dict[str, Finding],
    covered: dict[str, Finding],
    opposes_map: Mapping[str, str],
    turn_order: Mapping[str, int],
) -> tuple[set[str], list[LearnerEvent]]:
    """For each covered entity opposed by >= 1 contradiction, emit EXACTLY ONE
    conflict event and CONSUME the covered key + ALL its opposing misconception
    keys (so no standalone event double-fires on the same entity).

    The §6.5 table is written single-opposer; the MULTI-opposer case (two
    misconceptions opposing the SAME covered entity) is generalized here to
    last-position-wins against the LATEST opposing contradiction. This guarantees
    ONE event per covered entity — without it two opposers would emit two events
    on the same ``canonical_key``, breaking the one-event-per-entity contract and
    colliding with the WU-5A ``UNIQUE NULLS NOT DISTINCT (attempt_id, entity_id,
    event_kind)`` on ``apollo_mastery_events``. Single-opposer behavior is
    unchanged (one opposer -> the representative IS that opposer)."""
    # Group opposing contradictions by the covered entity they oppose.
    opposers_by_covered: dict[str, list[tuple[str, Finding]]] = {}
    for misc_key, contradiction in contradictions.items():
        covered_key = opposes_map.get(misc_key)
        if covered_key is None or covered_key not in covered:
            continue
        opposers_by_covered.setdefault(covered_key, []).append((misc_key, contradiction))

    consumed: set[str] = set()
    events: list[LearnerEvent] = []
    for covered_key in sorted(opposers_by_covered):  # deterministic
        opposers = opposers_by_covered[covered_key]
        covered_finding = covered[covered_key]
        # Representative opposer = the contradiction asserted LAST (max turn
        # position; ties + unknown-order broken by misc_key) — last-position-wins
        # compares the covered against the most-recent misconception.
        rep_misc_key, rep_contradiction = max(
            opposers, key=lambda mc: (_turn_position(mc[1], turn_order), mc[0])
        )
        events.append(
            _conflict_event(
                rep_misc_key,
                covered_key,
                rep_contradiction,
                covered_finding,
                turn_order,
            )
        )
        consumed.add(covered_key)
        for misc_key, _ in opposers:
            consumed.add(misc_key)
    return consumed, events


def _conflict_event(
    misc_key: str,
    covered_key: str,
    contradiction: Finding,
    covered: Finding,
    turn_order: Mapping[str, int],
) -> LearnerEvent:
    """Decide the opposed-pair event by turn order (last position wins)."""
    contradiction_turn = _turn_position(contradiction, turn_order)
    covered_turn = _turn_position(covered, turn_order)

    if contradiction_turn < covered_turn:
        # contradiction earlier, covered later -> the student CORRECTED the
        # misconception on the opposed entity.
        return LearnerEvent(
            canonical_key=covered_key,
            event_kind=LearnerEventKind.CORRECTED,
            score=covered.score if covered.score is not None else DEFAULT_COVERED_SCORE,
            confidence=covered.confidence,
            misconception_code=misc_key,
            evidence_node_ids=covered.student_node_ids + contradiction.student_node_ids,
            reference_step_id=covered_key,
        )
    if covered_turn < contradiction_turn:
        # covered earlier, contradiction later -> the misconception is the LAST
        # word; emit a misconception (last position wins).
        return LearnerEvent(
            canonical_key=misc_key,
            event_kind=LearnerEventKind.MISCONCEPTION,
            score=0.0,
            confidence=_coalesce_confidence(contradiction.confidence),
            misconception_code=misc_key,
            evidence_node_ids=contradiction.student_node_ids,
        )
    # equal / both sentinel -> order ambiguous -> partial + mixed flag.
    return LearnerEvent(
        canonical_key=covered_key,
        event_kind=LearnerEventKind.PARTIAL,
        score=AMBIGUOUS_ORDER_SCORE,
        confidence=AMBIGUOUS_ORDER_CONFIDENCE,
        misconception_code=misc_key,
        evidence_node_ids=covered.student_node_ids + contradiction.student_node_ids,
        diagnostic_flags=(MIXED_UNDERSTANDING_FLAG,),
    )


def _emit_standalone(
    contradictions: dict[str, Finding],
    covered: dict[str, Finding],
    missing: dict[str, Finding],
    consumed: set[str],
) -> list[LearnerEvent]:
    """Emit the per-entity standalone events for every key not consumed by a
    conflict pair."""
    events: list[LearnerEvent] = []
    for key, finding in contradictions.items():
        if key in consumed:
            continue
        events.append(_misconception_event(key, finding))
    for key, finding in covered.items():
        if key in consumed:
            continue
        events.append(_covered_event(key, finding))
    for key, finding in missing.items():
        if key in consumed:
            continue
        events.append(_missing_event(key, finding))
    return events


def _misconception_event(key: str, finding: Finding) -> LearnerEvent:
    """A standalone contradiction -> ``misconception`` (s=0.0)."""
    return LearnerEvent(
        canonical_key=key,
        event_kind=LearnerEventKind.MISCONCEPTION,
        score=0.0,
        confidence=_coalesce_confidence(finding.confidence),
        misconception_code=key,
        evidence_node_ids=finding.student_node_ids,
    )


def _missing_event(key: str, finding: Finding) -> LearnerEvent:
    """A negative-audit missing_node -> ``missing`` (s=0.0). The audit was
    negative — WU-4B1 already REWROTE audit-positive missings to COVERED_NODE
    upstream, so a surviving MISSING_NODE is genuinely uncovered."""
    return LearnerEvent(
        canonical_key=key,
        event_kind=LearnerEventKind.MISSING,
        score=0.0,
        reference_step_id=_first_or_none(finding.reference_node_ids),
    )


def _covered_event(key: str, finding: Finding) -> LearnerEvent:
    """A covered node -> ``covered``. An audit-upgraded covered (marker) lands at
    the capped confidence (<=0.75) so the §3 ``covered, s∈[0,1]`` row sits
    mid-band ("shaky", never a false 1.0); a plain covered scales by the
    resolution confidence."""
    confidence = finding.confidence
    if _is_audit_upgraded(finding):
        score = finding.score if finding.score is not None else confidence
    else:
        score = _plain_covered_score(finding)
    return LearnerEvent(
        canonical_key=key,
        event_kind=LearnerEventKind.COVERED,
        score=score,
        confidence=confidence,
        evidence_node_ids=finding.student_node_ids,
        reference_step_id=key,
    )


def _plain_covered_score(finding: Finding) -> float:
    """A plain covered node's score = ``finding.score`` else its resolution
    ``confidence`` else the default — scaled by resolution confidence."""
    if finding.score is not None:
        return finding.score
    if finding.confidence is not None:
        return finding.confidence
    return DEFAULT_COVERED_SCORE


def _coalesce_confidence(confidence: float | None) -> float:
    """Coalesce a finding's ``None`` confidence to the default misconception
    confidence (WU-4B1 already gated low-confidence misconceptions out)."""
    return confidence if confidence is not None else _DEFAULT_MISCONCEPTION_CONFIDENCE


def _first_or_none(ids: tuple[str, ...]) -> str | None:
    """The first id of a tuple, or None when empty (the missing finding's
    reference node anchors ``reference_step_id``)."""
    return ids[0] if ids else None


def _apply_suppression(
    events: list[LearnerEvent], suppressed: frozenset[str]
) -> list[LearnerEvent]:
    """Drop any event whose ``event_kind.value`` is in ``suppressed`` (the LAST
    step — filters FINAL event kinds; ``corrected`` survives a ``misconception``
    suppression because the values differ)."""
    if not suppressed:
        return events
    return [e for e in events if e.event_kind.value not in suppressed]
