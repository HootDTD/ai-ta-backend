"""WU-4B3 §6.11 / §6.9 executable corpus — the adversarial fixture table.

Each :class:`CorpusFixture` carries everything the WU-4B chain needs and
everything to assert: the pre-audit :class:`GradeResult`, a deterministic
:class:`ResolutionResult` (no live resolver), the parser ``student_nodes`` (the
min-parser-confidence abstention input), the closed ``candidates`` set (missing
entity lookup + opposes map), an injected deterministic ``audit_fn``, the
``turn_order`` map (node_id -> turn position), the ``ReferenceGraph`` (for the
reference hash), and the EXPECTED audited finding-kind + produced event-kind
multisets. ``persists=True`` rows are also exercised on real PG (the
``tests/database`` gate).

The fixtures REUSE the upstream unit builders (``_builders.py``) so the corpus
composes the SAME value objects 4A2 / 4B1 / 4B2 already test, rather than
re-deriving them. Each builder maps ONE §6.11 (or the §6.9 capstone) spec row to
its asserted findings AND events.

Pure data + deterministic stubs: NO live LLM, NO Neo4j, NO resolver call, NO PG
(persistence is exercised by the caller for ``persists=True`` rows).
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.grading.audited_grade import build_audited_grade
from apollo.grading.events import convert_findings_to_events
from apollo.grading.opposes import build_opposes_map
from apollo.grading.tests._builders import (
    candidate,
    covered_finding_with_nodes,
    found_audit_fn,
    misc_candidate,
    missing_grade,
    nodes_with_confidences,
    notfound_audit_fn,
    resolution_with,
    turn_order_of,
)
from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.ontology.edges import EdgeType
from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.result import ResolutionResult


@dataclass(frozen=True)
class CorpusFixture:
    """One §6.11 / §6.9 corpus row: chain inputs + the expected outputs."""

    name: str
    grade: object  # GradeResult (kept loose to avoid a circular type import)
    resolution: ResolutionResult
    student_nodes: tuple[Node, ...]
    candidates: tuple[Candidate, ...]
    audit_fn: object
    transcript: str
    turn_order: dict[str, int]
    reference_graph: ReferenceGraph
    expected_finding_kinds: tuple[str, ...]
    expected_event_kinds: tuple[str, ...]
    persists: bool

    def opposes_map(self):
        return build_opposes_map(self.candidates)

    def run_chain(self):
        """Run ``build_audited_grade -> convert_findings_to_events`` deterministically.

        Returns ``(audited, events)``."""
        audited = build_audited_grade(
            self.grade,
            transcript=self.transcript,
            resolution=self.resolution,
            student_nodes=self.student_nodes,
            candidates=self.candidates,
            audit_fn=self.audit_fn,
        )
        events = convert_findings_to_events(
            audited,
            opposes_map=self.opposes_map(),
            turn_order=self.turn_order,
        )
        return audited, events


# ---------------------------------------------------------------------------
# Reference-graph helpers (for the reference hash; shape, not content, matters).
# ---------------------------------------------------------------------------


def _ref_node(key: str, node_type: str = "condition", symbolic: str | None = None) -> CanonicalNode:
    return CanonicalNode(
        canonical_key=key,
        node_type=node_type,  # type: ignore[arg-type]
        source_node_ids=(f"step-{key}",),
        evidence_spans=(),
        symbolic=symbolic,
        method=None,
        confidence=None,
    )


def _ref(*keys: str) -> ReferenceGraph:
    nodes = tuple(_ref_node(k) for k in keys)
    edges: tuple[CanonicalEdge, ...] = tuple(
        CanonicalEdge(
            edge_type=EdgeType.DEPENDS_ON,
            from_key=keys[i - 1],
            to_key=keys[i],
            provenance="explicit",
        )
        for i in range(1, len(keys))
    )
    paths = (ReferencePathView(canonical_keys=tuple(keys)),)
    return ReferenceGraph(nodes=nodes, edges=edges, paths=paths)


def _sorted_kinds(*kinds: str) -> tuple[str, ...]:
    return tuple(sorted(kinds))


# Shared parser nodes at a confident parser tier (so the min-parser-confidence
# gate is NOT tripped unless a fixture deliberately lowers it).
def _confident_nodes(n: int = 1) -> tuple[Node, ...]:
    return nodes_with_confidences(*([0.95] * n))


# ---------------------------------------------------------------------------
# §6.11 fixtures — one builder per spec row.
# ---------------------------------------------------------------------------


def _f_valid_alternative_path() -> CorpusFixture:
    """Valid alternative path (energy, not Bernoulli): covered via path B + an
    ``alternative_path`` finding; zero false missing. Events: covered ×N, no
    missing."""
    grade = missing_grade(covered=("eq.energy", "cond.steady"))
    # inject the alternative_path finding (a winning-path != 0 marker).
    alt = Finding(
        kind=FindingKind.ALTERNATIVE_PATH,
        reference_node_ids=("eq.energy", "cond.steady"),
        message="student took declared alternative path 1",
    )
    grade = _with_extra(grade, alt)
    return CorpusFixture(
        name="valid_alternative_path",
        grade=grade,
        resolution=resolution_with(resolved=2),
        student_nodes=_confident_nodes(2),
        candidates=(candidate("eq.energy"), candidate("cond.steady")),
        audit_fn=notfound_audit_fn(),
        transcript="energy method ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("cond.steady", "eq.energy"),
        expected_finding_kinds=_sorted_kinds("covered_node", "covered_node", "alternative_path"),
        expected_event_kinds=_sorted_kinds("covered", "covered"),
        persists=False,
    )


def _f_thin_explanation() -> CorpusFixture:
    """Correct answer, thin explanation: low coverage, high soundness, no
    contradiction. Events: covered (few), no misconception."""
    grade = missing_grade(covered=("eq.bernoulli",))
    return CorpusFixture(
        name="thin_explanation",
        grade=grade,
        resolution=resolution_with(resolved=1),
        student_nodes=_confident_nodes(1),
        candidates=(candidate("eq.bernoulli"),),
        audit_fn=notfound_audit_fn(),
        transcript="just the final equation ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("covered_node"),
        expected_event_kinds=_sorted_kinds("covered"),
        persists=False,
    )


def _f_wrong_answer_mostly_correct() -> CorpusFixture:
    """Wrong answer, mostly-correct concepts: covered + a contradiction on the
    final relation. Events: covered + misconception."""
    grade = missing_grade(
        covered=("cond.incompressibility",),
        contradictions=(("misc.final_relation", ("m1",)),),
    )
    return CorpusFixture(
        name="wrong_answer_mostly_correct",
        grade=grade,
        resolution=resolution_with(resolved=1, resolved_nodes=(("m1", 0.95),)),
        student_nodes=_confident_nodes(2),
        candidates=(
            candidate("cond.incompressibility"),
            misc_candidate("misc.final_relation", opposes=None),
        ),
        audit_fn=notfound_audit_fn(),
        transcript="... wrong final step ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("cond.incompressibility", "eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("covered_node", "contradiction"),
        expected_event_kinds=_sorted_kinds("covered", "misconception"),
        persists=False,
    )


def _f_polar_near_miss() -> CorpusFixture:
    """Polar near-miss ("pressure increases with speed"): resolves to ``misc.*``
    -> contradiction (NOT covered on the lexically-close ref key). Events:
    misconception (on the misc key, never the reference key)."""
    grade = missing_grade(contradictions=(("misc.pressure_speed", ("m1",)),))
    return CorpusFixture(
        name="polar_near_miss",
        grade=grade,
        resolution=resolution_with(resolved=1, resolved_nodes=(("m1", 0.9),)),
        student_nodes=_confident_nodes(1),
        candidates=(
            candidate("cond.pressure_speed"),  # the lexically-close reference key
            misc_candidate("misc.pressure_speed", opposes=None),
        ),
        audit_fn=notfound_audit_fn(),
        transcript="pressure increases with speed ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("cond.pressure_speed"),
        expected_finding_kinds=_sorted_kinds("contradiction"),
        expected_event_kinds=_sorted_kinds("misconception"),
        persists=False,
    )


def _f_conflict_misconception_then_correct() -> CorpusFixture:
    """Conflict: misconception FIRST, then correct -> the student CORRECTED it.
    Events: ``corrected``."""
    grade = missing_grade(
        covered=(),
        contradictions=(("misc.x", ("m1",)),),
    )
    grade = _with_extra(grade, covered_finding_with_nodes("cond.real", ("c1",)))
    return CorpusFixture(
        name="conflict_misconception_then_correct",
        grade=grade,
        resolution=resolution_with(resolved=2, resolved_nodes=(("m1", 0.95),)),
        student_nodes=_confident_nodes(2),
        candidates=(
            candidate("cond.real"),
            misc_candidate("misc.x", opposes="cond.real"),
        ),
        audit_fn=notfound_audit_fn(),
        transcript="... first wrong, then corrected ...",
        # misconception earlier (turn 1), covered later (turn 2) -> corrected.
        turn_order=turn_order_of(m1=1, c1=2),
        reference_graph=_ref("cond.real"),
        expected_finding_kinds=_sorted_kinds("covered_node", "contradiction"),
        expected_event_kinds=_sorted_kinds("corrected"),
        persists=False,
    )


def _f_conflict_correct_then_misconception() -> CorpusFixture:
    """Conflict: correct FIRST, then misconception -> last position wins. Events:
    ``misconception``."""
    grade = missing_grade(contradictions=(("misc.x", ("m1",)),))
    grade = _with_extra(grade, covered_finding_with_nodes("cond.real", ("c1",)))
    return CorpusFixture(
        name="conflict_correct_then_misconception",
        grade=grade,
        resolution=resolution_with(resolved=2, resolved_nodes=(("m1", 0.95),)),
        student_nodes=_confident_nodes(2),
        candidates=(
            candidate("cond.real"),
            misc_candidate("misc.x", opposes="cond.real"),
        ),
        audit_fn=notfound_audit_fn(),
        transcript="... first right, then wrong ...",
        # covered earlier (turn 1), misconception later (turn 2) -> misconception.
        turn_order=turn_order_of(c1=1, m1=2),
        reference_graph=_ref("cond.real"),
        expected_finding_kinds=_sorted_kinds("covered_node", "contradiction"),
        expected_event_kinds=_sorted_kinds("misconception"),
        persists=False,
    )


def _f_vague_pronouns() -> CorpusFixture:
    """Vague pronouns ("it increases there"): an ``unresolved`` finding, no
    event-bearing finding. Events: () (counts toward abstention but here a single
    unresolved over 1 node = rate 1.0 would abstain — keep it BELOW threshold by
    mixing a resolved node so the run is diagnostic-only without abstaining)."""
    grade = missing_grade()
    grade = _with_extra(
        grade,
        Finding(
            kind=FindingKind.UNRESOLVED,
            student_node_ids=("u1",),
            evidence_spans=("it increases there",),
        ),
    )
    return CorpusFixture(
        name="vague_pronouns",
        grade=grade,
        # 1 unresolved + 2 resolved -> rate 0.33 < 0.35 (no abstention).
        resolution=resolution_with(resolved=2, unresolved=1),
        student_nodes=_confident_nodes(1),
        candidates=(),
        audit_fn=notfound_audit_fn(),
        transcript="it increases there ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("cond.any"),
        expected_finding_kinds=_sorted_kinds("unresolved"),
        expected_event_kinds=(),
        persists=False,
    )


def _f_nonstandard_notation() -> CorpusFixture:
    """Nonstandard notation / paraphrase: covered at alias/symbolic tier. Events:
    covered."""
    grade = missing_grade(covered=("eq.bernoulli",))
    return CorpusFixture(
        name="nonstandard_notation",
        grade=grade,
        resolution=resolution_with(resolved=1),
        student_nodes=_confident_nodes(1),
        candidates=(candidate("eq.bernoulli", aliases=("P + half rho v squared",)),),
        audit_fn=notfound_audit_fn(),
        transcript="P plus half rho v squared equals constant ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("covered_node"),
        expected_event_kinds=_sorted_kinds("covered"),
        persists=False,
    )


def _f_parser_misses_key_sentence() -> CorpusFixture:
    """Parser misses a key sentence: the audit upgrades missing->covered (<=0.75),
    NO false missing. Events: covered <=0.75. PERSISTS."""
    grade = missing_grade(("cond.steady_flow",))
    audit_fn = found_audit_fn({"cond.steady_flow": "the student said the flow is steady"})
    return CorpusFixture(
        name="parser_misses_key_sentence",
        grade=grade,
        resolution=resolution_with(resolved=1),
        student_nodes=_confident_nodes(1),
        candidates=(candidate("cond.steady_flow", display_name="Steady flow"),),
        audit_fn=audit_fn,
        transcript="the flow is steady throughout ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("cond.steady_flow", "eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("covered_node"),
        expected_event_kinds=_sorted_kinds("covered"),
        persists=True,
    )


def _f_reference_omits_valid_assumption() -> CorpusFixture:
    """Reference omits a valid stated assumption: ``unsupported_extra``, zero
    soundness penalty. Events: ()."""
    grade = missing_grade()
    grade = _with_extra(
        grade,
        Finding(
            kind=FindingKind.UNSUPPORTED_EXTRA,
            canonical_key="cond.extra_valid",
            student_node_ids=("x1",),
        ),
    )
    return CorpusFixture(
        name="reference_omits_valid_assumption",
        grade=grade,
        resolution=resolution_with(resolved=1),
        student_nodes=_confident_nodes(1),
        candidates=(),
        audit_fn=notfound_audit_fn(),
        transcript="also assuming inviscid flow ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("unsupported_extra"),
        expected_event_kinds=(),
        persists=False,
    )


def _f_misconception_not_in_bank() -> CorpusFixture:
    """Misconception not in ``misc.*``: ``unsupported_extra`` (honest
    non-detection). Events: ()."""
    grade = missing_grade()
    grade = _with_extra(
        grade,
        Finding(
            kind=FindingKind.UNSUPPORTED_EXTRA,
            canonical_key="cond.unknown_belief",
            student_node_ids=("x1",),
        ),
    )
    return CorpusFixture(
        name="misconception_not_in_bank",
        grade=grade,
        resolution=resolution_with(resolved=1),
        student_nodes=_confident_nodes(1),
        candidates=(),
        audit_fn=notfound_audit_fn(),
        transcript="some belief not in the bank ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("unsupported_extra"),
        expected_event_kinds=(),
        persists=False,
    )


def _f_high_unresolved_abstains() -> CorpusFixture:
    """High-unresolved-rate (>0.35): findings persisted, ``abstained=True``.
    Events: () (no Layer-3 update). PERSISTS."""
    grade = missing_grade(covered=("eq.bernoulli",))
    # add an unresolved finding so a row is written even on abstention.
    grade = _with_extra(
        grade,
        Finding(kind=FindingKind.UNRESOLVED, student_node_ids=("u1",), evidence_spans=("vague",)),
    )
    return CorpusFixture(
        name="high_unresolved_abstains",
        grade=grade,
        # 3 unresolved / 4 total = 0.75 > 0.35 -> abstained.
        resolution=resolution_with(resolved=1, unresolved=3),
        student_nodes=_confident_nodes(1),
        candidates=(candidate("eq.bernoulli"),),
        audit_fn=notfound_audit_fn(),
        transcript="mostly unparseable ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("eq.bernoulli"),
        expected_finding_kinds=_sorted_kinds("covered_node", "unresolved"),
        expected_event_kinds=(),  # abstained -> no events
        persists=True,
    )


def _f_bernoulli_capstone() -> CorpusFixture:
    """§6.9 Bernoulli capstone. v1 events: covered ×3 + missing. PERSISTS.

    DELIBERATE DEVIATION from the §6.9 NARRATIVE (spec line 877 lists
    'covered ×2, partial (velocity), missing'): v1's frozen §6.5 table (WU-4B2)
    has NO standalone-``partial`` path for a shaky covered — the edge-gap
    ``partial`` variant is calibration-gated OFF (``PARTIAL_EDGE_GAP_ENABLED =
    False``, §6.2: an edge gap must never halve a score), and an audited span
    upgrades to COVERED ≤0.75, not ``partial``. So the narrative's
    ``partial (velocity)`` surfaces in v1 as an audit-upgraded COVERED at ≤0.75 —
    a low-confidence ("shaky") covered (the §3 covered s∈[0,1] mid-band), i.e. a
    THIRD covered, not a partial. The two plain covered are full-confidence; one
    missing key survives the audit (negative) -> a missing event.
    ``test_bernoulli_capstone_events`` pins the deviation (exactly ONE covered at
    ≤0.75 — the would-be-partial). Flipping ``PARTIAL_EDGE_GAP_ENABLED`` on after
    calibration is what would realize the narrative's standalone partial."""
    grade = missing_grade(
        covered=("eq.bernoulli", "eq.continuity"),
        keys=("cond.assumptions", "proc.solve"),
    )
    # audit upgrades cond.assumptions -> covered <=0.75 ('partial' band); proc.solve
    # stays missing (audit negative).
    audit_fn = found_audit_fn(
        {"cond.assumptions": "the student stated the steady inviscid assumptions"}
    )
    return CorpusFixture(
        name="bernoulli_capstone",
        grade=grade,
        resolution=resolution_with(resolved=2),
        student_nodes=_confident_nodes(2),
        candidates=(
            candidate("eq.bernoulli"),
            candidate("eq.continuity"),
            candidate("cond.assumptions", display_name="Assumptions"),
            candidate("proc.solve", display_name="Solve"),
        ),
        audit_fn=audit_fn,
        transcript="bernoulli + continuity + the steady inviscid assumptions ...",
        turn_order=turn_order_of(),
        reference_graph=_ref("cond.assumptions", "eq.continuity", "eq.bernoulli", "proc.solve"),
        # audited findings: 2 plain covered + 1 upgraded covered + 1 surviving missing.
        expected_finding_kinds=_sorted_kinds(
            "covered_node", "covered_node", "covered_node", "missing_node"
        ),
        # events: 3 covered + 1 missing.
        expected_event_kinds=_sorted_kinds("covered", "covered", "covered", "missing"),
        persists=True,
    )


def _with_extra(grade, *extra: Finding):
    """Return a NEW GradeResult with ``extra`` findings appended (immutable)."""
    import dataclasses

    return dataclasses.replace(grade, findings=grade.findings + tuple(extra))


# The full ordered corpus (12 §6.11 rows + the §6.9 capstone).
def build_corpus() -> tuple[CorpusFixture, ...]:
    return (
        _f_valid_alternative_path(),
        _f_thin_explanation(),
        _f_wrong_answer_mostly_correct(),
        _f_polar_near_miss(),
        _f_conflict_misconception_then_correct(),
        _f_conflict_correct_then_misconception(),
        _f_vague_pronouns(),
        _f_nonstandard_notation(),
        _f_parser_misses_key_sentence(),
        _f_reference_omits_valid_assumption(),
        _f_misconception_not_in_bank(),
        _f_high_unresolved_abstains(),
        _f_bernoulli_capstone(),
    )


CORPUS: tuple[CorpusFixture, ...] = build_corpus()

# The persistence-touching subset (exercised on real PG by the tests/database gate).
PERSISTING_CORPUS: tuple[CorpusFixture, ...] = tuple(f for f in CORPUS if f.persists)
