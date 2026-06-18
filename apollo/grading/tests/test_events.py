"""WU-4B2 — the §6.5 finding->event decision table tests.

ONE discriminating test per BINDING row + the abstention/suppression honoring +
determinism/purity. All PURE: zero DB/LLM/Neo4j/network. The marker-keying row
builds the AuditedGrade via the REAL WU-4B1 ``build_audited_grade`` (with a
deterministic stub ``audit_fn``) to PROVE the audit-upgrade row keys on the
``AUDIT_UPGRADE_MESSAGE`` marker, not on kind/confidence.
"""

from __future__ import annotations

from apollo.grading.audited_grade import AUDIT_UPGRADE_MESSAGE, build_audited_grade
from apollo.grading.event_model import LearnerEventKind
from apollo.grading.events import (
    AMBIGUOUS_ORDER_CONFIDENCE,
    AMBIGUOUS_ORDER_SCORE,
    MIXED_UNDERSTANDING_FLAG,
    PARTIAL_EDGE_GAP_ENABLED,
    convert_findings_to_events,
)
from apollo.graph_compare.findings import Finding, FindingKind

from ._builders import (
    audited,
    contradiction_finding,
    covered_finding,
    covered_finding_with_nodes,
    missing_finding,
    missing_grade,
    nodes_with_confidences,
    resolution_with,
    turn_order_of,
)

# --- §6.5 row-by-row ---------------------------------------------------------


def test_covered_node_with_edges_emits_covered():
    """Row 1: a plain covered node -> ONE covered event scaled by resolution
    confidence; reference_step_id == key; evidence == student_node_ids."""
    grade = audited((covered_finding_with_nodes("eq.b", ("s1",), confidence=0.98),))

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.COVERED
    assert event.canonical_key == "eq.b"
    assert event.score == 0.98
    assert event.confidence == 0.98
    assert event.reference_step_id == "eq.b"
    assert event.evidence_node_ids == ("s1",)


def test_covered_node_with_edge_missing_stays_covered_not_partial():
    """Row 2: a covered node + a missing_edge diagnostic finding stays ``covered``
    (NOT partial) — an edge gap must never downgrade a node (§6.2). The gate
    constant is asserted False so flipping it is a test failure."""
    assert PARTIAL_EDGE_GAP_ENABLED is False
    grade = audited(
        (
            covered_finding_with_nodes("eq.b", ("s1",), confidence=0.92),
            Finding(kind=FindingKind.MISSING_EDGE, message="eq.b -USES-> var.p"),
        )
    )

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    assert events[0].event_kind is LearnerEventKind.COVERED
    assert events[0].diagnostic_flags == ()


def test_missing_node_audit_negative_emits_missing_s0():
    """Row 3: a genuine negative-audit missing_node -> ONE missing event, s=0.0,
    reference_step_id = the missing finding's reference node id, NO covered."""
    missing = Finding(
        kind=FindingKind.MISSING_NODE,
        canonical_key="eq.b",
        reference_node_ids=("r1",),
        score=0.0,
    )
    grade = audited((missing,))

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.MISSING
    assert event.score == 0.0
    assert event.reference_step_id == "r1"


def test_missing_node_audit_span_emits_covered_keyed_on_marker():
    """Row 4: an audit-found missing_node (built by the REAL build_audited_grade
    with a deterministic stub audit_fn) -> a COVERED event at confidence <= 0.75.
    The path keys on AUDIT_UPGRADE_MESSAGE: swap the message and the SAME finding
    routes as a plain covered (proving marker-keying, not confidence)."""

    def found_audit(request):
        return {e.canonical_key: "the quoted span" for e in request.entities}

    grade = missing_grade(("eq.b",))
    student_nodes = nodes_with_confidences(0.9)
    audited_grade = build_audited_grade(
        grade,
        transcript="... the student said the quoted span ...",
        resolution=resolution_with(resolved=1),
        student_nodes=student_nodes,
        candidates=(),
        audit_fn=found_audit,
    )
    # sanity: the rewritten finding carries the marker + the capped confidence
    upgraded = audited_grade.findings[0]
    assert upgraded.message == AUDIT_UPGRADE_MESSAGE
    assert upgraded.confidence == 0.75

    events = convert_findings_to_events(audited_grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.COVERED
    assert event.confidence is not None and event.confidence <= 0.75
    assert event.canonical_key == "eq.b"

    # DISCRIMINATOR: a MISSING_NODE-origin finding only becomes covered via the
    # marker. Swap the message away and it routes as a missing (not covered).
    without_marker = audited(
        (
            Finding(
                kind=FindingKind.MISSING_NODE,
                canonical_key="eq.b",
                reference_node_ids=("r1",),
                score=0.0,
                confidence=0.75,
                message="something else",
            ),
        )
    )
    swapped = convert_findings_to_events(without_marker, opposes_map={}, turn_order={})
    assert swapped[0].event_kind is LearnerEventKind.MISSING


def test_contradiction_emits_misconception_s0():
    """Row 5: a standalone contradiction -> ONE misconception event, s=0.0,
    misconception_code == key, evidence == student_node_ids."""
    grade = audited((contradiction_finding("misc.x", student_node_ids=("n1",)),))

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.MISCONCEPTION
    assert event.score == 0.0
    assert event.misconception_code == "misc.x"
    assert event.evidence_node_ids == ("n1",)


def test_conflict_contradiction_earlier_covered_later_emits_corrected():
    """Row 6: contradiction-earlier + covered-later on the opposed entity -> ONE
    ``corrected`` event on the OPPOSED entity (covered_key); NO standalone
    misconception/covered for the pair."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=2),
    )

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.CORRECTED
    assert event.canonical_key == "eq.y"
    assert event.misconception_code == "misc.x"


def test_conflict_covered_earlier_contradiction_later_emits_misconception():
    """Row 7: SWAP the turn order -> ONE ``misconception`` (last position wins);
    NO corrected. Proves last-position-wins discriminates."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(v1=1, c1=2),
    )

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.MISCONCEPTION
    assert event.misconception_code == "misc.x"


def test_multiple_opposers_same_covered_emits_single_event():
    """Regression (review MEDIUM): TWO misconceptions opposing the SAME covered
    entity must emit EXACTLY ONE event on that entity — not one per opposer. Two
    `corrected` events on the same canonical_key would break one-event-per-entity
    and collide with the WU-5A UNIQUE (attempt_id, entity_id, event_kind)."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            contradiction_finding("misc.z", student_node_ids=("c2",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y", "misc.z": "eq.y"},  # both oppose eq.y
        turn_order=turn_order_of(c1=1, c2=2, v1=3),  # both contradictions earlier
    )

    # EXACTLY ONE event on eq.y (corrected), never two.
    assert len(events) == 1
    (event,) = events
    assert event.canonical_key == "eq.y"
    assert event.event_kind is LearnerEventKind.CORRECTED


def test_multiple_opposers_mixed_order_last_position_wins():
    """Two opposers of eq.y, one BEFORE and one AFTER the covered: the LATEST
    contradiction (misc.z) is the representative -> last-position-wins emits ONE
    `misconception` on misc.z; eq.y + misc.x are consumed (no extra events)."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),  # before
            contradiction_finding("misc.z", student_node_ids=("c2",)),  # after
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y", "misc.z": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=2, c2=3),  # misc.x < covered < misc.z
    )

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.MISCONCEPTION
    assert event.misconception_code == "misc.z"  # the LATEST opposer wins


def test_multiple_opposers_equal_turn_tie_break_is_deterministic():
    """Two opposers of eq.y at the SAME turn position: the representative is the
    deterministic `max` tie-break on misc_key (misc.b > misc.a), so the single
    `corrected` event's misconception_code is stable run-to-run."""
    grade = audited(
        (
            contradiction_finding("misc.a", student_node_ids=("c1",)),
            contradiction_finding("misc.b", student_node_ids=("c2",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.a": "eq.y", "misc.b": "eq.y"},
        turn_order=turn_order_of(c1=1, c2=1, v1=2),  # both opposers at turn 1 (tie)
    )

    assert len(events) == 1
    (event,) = events
    assert event.canonical_key == "eq.y"
    assert event.event_kind is LearnerEventKind.CORRECTED
    assert event.misconception_code == "misc.b"  # deterministic misc_key tie-break


def test_opposes_map_target_absent_from_covered_emits_standalone_misconception():
    """Condition-coverage (review test-honesty nit): an opposes_map entry whose
    covered target is ABSENT from the grade exercises the `covered_key not in
    covered` operand -> NOT a conflict -> the contradiction emits a standalone
    misconception (not consumed)."""
    grade = audited((contradiction_finding("misc.x", student_node_ids=("c1",)),))

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.absent"},  # eq.absent is NOT a covered finding
        turn_order=turn_order_of(c1=1),
    )

    assert len(events) == 1
    (event,) = events
    assert event.canonical_key == "misc.x"
    assert event.event_kind is LearnerEventKind.MISCONCEPTION


def test_conflict_ambiguous_order_emits_partial_with_mixed_flag():
    """Row 8: equal turn positions (ambiguous) -> ONE ``partial`` at low
    confidence + the mixed-understanding diagnostic flag."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=1),
    )

    assert len(events) == 1
    (event,) = events
    assert event.event_kind is LearnerEventKind.PARTIAL
    assert event.confidence == AMBIGUOUS_ORDER_CONFIDENCE
    assert event.score == AMBIGUOUS_ORDER_SCORE
    assert event.diagnostic_flags == (MIXED_UNDERSTANDING_FLAG,)


def test_unsupported_extra_emits_no_event():
    """Row 9: an unsupported_extra-only grade -> () (diagnostic-only)."""
    grade = audited(
        (
            Finding(
                kind=FindingKind.UNSUPPORTED_EXTRA,
                canonical_key="x",
                student_node_ids=("s1",),
            ),
        )
    )

    assert convert_findings_to_events(grade, opposes_map={}, turn_order={}) == ()


def test_unresolved_emits_no_event():
    """Row 10: an unresolved-only grade -> () (counts toward abstention, already
    consumed by WU-4B1)."""
    grade = audited(
        (Finding(kind=FindingKind.UNRESOLVED, student_node_ids=("u1",)),)
    )

    assert convert_findings_to_events(grade, opposes_map={}, turn_order={}) == ()


def test_edge_and_alternative_path_findings_emit_no_event():
    """Rows 9/10 extension: matched_edge / missing_edge / alternative_path
    findings produce no events (diagnostic-only)."""
    grade = audited(
        (
            Finding(kind=FindingKind.MATCHED_EDGE, message="a -USES-> b"),
            Finding(kind=FindingKind.MISSING_EDGE, message="a -USES-> c"),
            Finding(
                kind=FindingKind.ALTERNATIVE_PATH,
                reference_node_ids=("eq.a", "eq.b"),
                message="alt path 1",
            ),
        )
    )

    assert convert_findings_to_events(grade, opposes_map={}, turn_order={}) == ()


# --- abstention + suppression (§6.6 honoring 4B2 owns) -----------------------


def test_abstained_grade_returns_empty():
    """abstained=True -> () regardless of findings (no learner update at all)."""
    grade = audited(
        (
            covered_finding("eq.a", confidence=0.9),
            missing_finding("eq.b"),
        ),
        abstained=True,
    )

    assert convert_findings_to_events(grade, opposes_map={}, turn_order={}) == ()


def test_suppressed_missing_drops_missing_keeps_covered():
    """suppressed={'missing'} drops the missing event but keeps the covered."""
    grade = audited(
        (
            covered_finding_with_nodes("eq.a", ("s1",), confidence=0.9),
            Finding(
                kind=FindingKind.MISSING_NODE,
                canonical_key="eq.b",
                reference_node_ids=("r1",),
                score=0.0,
            ),
        ),
        suppressed=frozenset({"missing"}),
    )

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    kinds = {e.event_kind for e in events}
    assert kinds == {LearnerEventKind.COVERED}


def test_suppressed_missing_keeps_corrected_and_covered():
    """A missing suppression does NOT drop a corrected event (distinct kind)."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
            Finding(
                kind=FindingKind.MISSING_NODE,
                canonical_key="eq.z",
                reference_node_ids=("r1",),
                score=0.0,
            ),
        ),
        suppressed=frozenset({"missing"}),
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=2),
    )

    kinds = {e.event_kind for e in events}
    assert LearnerEventKind.CORRECTED in kinds
    assert LearnerEventKind.MISSING not in kinds


def test_suppressed_misconception_drops_misconception_not_corrected():
    """suppressed={'misconception'} drops the standalone misconception but a
    corrected event SURVIVES ('corrected' is NOT 'misconception')."""
    grade = audited(
        (
            # standalone contradiction -> misconception (dropped)
            contradiction_finding("misc.standalone", student_node_ids=("n1",)),
            # conflict pair -> corrected (survives)
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        ),
        suppressed=frozenset({"misconception"}),
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=2),
    )

    kinds = {e.event_kind for e in events}
    assert kinds == {LearnerEventKind.CORRECTED}


def test_suppression_applies_after_conflict_resolution():
    """A misconception suppression DROPS a conflict-row misconception
    (covered-earlier-contradiction-later is a misconception, subject to it)."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        ),
        suppressed=frozenset({"misconception"}),
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(v1=1, c1=2),  # covered-first -> misconception
    )

    assert events == ()


# --- determinism + purity ----------------------------------------------------


def test_output_is_deterministically_ordered():
    """Two covered findings on keys b, a -> events sorted (a, b); twice equal."""
    grade = audited(
        (
            covered_finding_with_nodes("b", ("s2",), confidence=0.9),
            covered_finding_with_nodes("a", ("s1",), confidence=0.9),
        )
    )

    first = convert_findings_to_events(grade, opposes_map={}, turn_order={})
    second = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert [e.canonical_key for e in first] == ["a", "b"]
    assert first == second


def test_inputs_are_not_mutated():
    """The input findings tuple, opposes_map, and turn_order are unchanged."""
    findings = (
        contradiction_finding("misc.x", student_node_ids=("c1",)),
        covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
    )
    grade = audited(findings)
    opposes_map = {"misc.x": "eq.y"}
    turn_order = turn_order_of(c1=1, v1=2)

    convert_findings_to_events(grade, opposes_map=opposes_map, turn_order=turn_order)

    assert grade.findings is findings
    assert grade.findings == findings
    assert opposes_map == {"misc.x": "eq.y"}
    assert turn_order == {"c1": 1, "v1": 2}


def test_turn_position_uses_min_over_student_node_ids():
    """A finding spanning nodes (n2, n1) anchors to the EARLIEST (min) turn — the
    decision flips to corrected; with max it would be a misconception."""
    grade = audited(
        (
            # contradiction spans n2(turn5) + n1(turn1) -> min = turn 1
            contradiction_finding("misc.x", student_node_ids=("n2", "n1")),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(n1=1, n2=5, v1=3),  # min(misc)=1 < covered=3
    )

    assert len(events) == 1
    assert events[0].event_kind is LearnerEventKind.CORRECTED


def test_missing_turn_order_node_treated_as_ambiguous_sentinel():
    """A conflict where a node id is ABSENT from turn_order -> +inf sentinel ->
    ambiguous -> partial + mixed flag (the defensive sentinel branch)."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order={},  # neither id present -> both +inf -> ambiguous
    )

    assert len(events) == 1
    assert events[0].event_kind is LearnerEventKind.PARTIAL
    assert events[0].diagnostic_flags == (MIXED_UNDERSTANDING_FLAG,)


def test_conflict_finding_with_no_node_ids_is_sentinel_ambiguous():
    """A conflict finding carrying NO student_node_ids -> the empty-positions
    sentinel branch -> ambiguous -> partial (covers the no-node-ids path)."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=()),  # empty
            covered_finding_with_nodes("eq.y", (), confidence=0.92),  # empty
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=2),  # ids irrelevant — findings empty
    )

    assert len(events) == 1
    assert events[0].event_kind is LearnerEventKind.PARTIAL


def test_plain_covered_uses_finding_score_when_present():
    """A plain covered finding carrying an explicit ``score`` uses it directly
    (the finding.score-not-None branch), NOT the confidence fallback."""
    grade = audited(
        (
            Finding(
                kind=FindingKind.COVERED_NODE,
                canonical_key="eq.b",
                student_node_ids=("s1",),
                score=0.4,
                confidence=0.92,
            ),
        )
    )

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    assert events[0].event_kind is LearnerEventKind.COVERED
    assert events[0].score == 0.4  # finding.score wins over confidence
    assert events[0].confidence == 0.92


def test_plain_covered_defaults_score_when_no_score_or_confidence():
    """A plain covered finding with neither score nor confidence -> the default
    covered score (the both-None fallback branch)."""
    from apollo.grading.events import DEFAULT_COVERED_SCORE

    grade = audited(
        (
            Finding(
                kind=FindingKind.COVERED_NODE,
                canonical_key="eq.b",
                student_node_ids=("s1",),
            ),
        )
    )

    events = convert_findings_to_events(grade, opposes_map={}, turn_order={})

    assert len(events) == 1
    assert events[0].score == DEFAULT_COVERED_SCORE
    assert events[0].confidence is None


def test_missing_on_consumed_conflict_entity_is_skipped():
    """A missing_node finding whose key is the opposed entity CONSUMED by a
    conflict pair is skipped (the consumed-missing branch) — the conflict event
    REPLACES it, no double-emit."""
    grade = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("c1",)),
            covered_finding_with_nodes("eq.y", ("v1",), confidence=0.92),
            # a missing finding on the SAME opposed entity eq.y -> consumed
            Finding(
                kind=FindingKind.MISSING_NODE,
                canonical_key="eq.y",
                reference_node_ids=("r1",),
                score=0.0,
            ),
        )
    )

    events = convert_findings_to_events(
        grade,
        opposes_map={"misc.x": "eq.y"},
        turn_order=turn_order_of(c1=1, v1=2),  # contradiction earlier -> corrected
    )

    assert len(events) == 1
    assert events[0].event_kind is LearnerEventKind.CORRECTED
    # the missing on eq.y was consumed (skipped), not emitted
    assert all(e.event_kind is not LearnerEventKind.MISSING for e in events)
