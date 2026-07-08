"""Campaign-plan Task A2 — pure canonical-artifact builders (spec §1).

``build_graph_artifact`` / ``build_llm_artifact`` turn an already-computed grade
(``ShadowGradeResult`` for the graph path, a coverage/rubric dict pair for the
LLM path) into the artifact payload dict a ``GradingArtifact`` row is minted
from. Both are PURE: no DB/Neo4j/LLM imports, no IO — they only reshape
dataclasses/dicts that the caller already built. Task A3 (the Done-click
writer) supplies the identity fields this module does NOT know (``attempt_id``,
``user_id``, ``search_space_id``, ``concept_id``, ``problem_id``, ``role``) and
merges them with the dict returned here before persisting.

Node-ledger construction (Task A2 Step 4): a reference/misconception key earns
one ``credited``/``misconception`` row per ``COVERED_NODE``/``CONTRADICTION``
finding in ``shadow.audited.findings`` (the post-audit-rewrite set — the same
findings ``persist_comparison_run`` writes), with the resolution METHOD looked
up from ``shadow.resolution`` by matching ``ResolvedNode.resolved_key ==
finding.canonical_key`` (the resolver is the only place that method lives — a
``Finding`` has no ``method`` field). An ``UNRESOLVED`` finding (a student
utterance that matched nothing) earns one ``unresolved`` row keyed by the
STUDENT node id (there is no reference key to show — the utterance matched no
candidate).

Scorecard hardening (campaign-plan Task 3, spec §2 "missing or unclear"):
``MISSING_NODE`` findings (a reference node with ZERO student evidence —
nothing was said) ALSO earn one ``unresolved`` row, keyed by the REFERENCE
node's own display-safe ``canonical_key`` (``missing_finding`` already sets
this from ``ref_node.canonical_key`` — never an internal student-side id) with
``evidence_span=None`` (not ``""``) so a renderer can tell "never mentioned"
apart from an ``UNRESOLVED`` utterance that carries a (possibly empty) surface
span. Without this row, the scorecard's "missing or unclear" rubric block
(``apollo.projections.scorecard._missing_or_unclear``) had literally nothing
to show for a reference concept the student never touched at all — it is
reflected in the ``node_coverage`` SCORE, but was invisible in the STUDENT-
FACING ledger the scorecard renders from (spec §2: "nothing computed fresh" —
the ledger has to carry it, since the scorecard cannot look at ``node_coverage``
and reconstruct which node was missing).
"""

from __future__ import annotations

import logging

from apollo.grading.composite import MISC_CONFIDENCE_FLOOR, CompositeWeights, composite_score
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.overseer.misconception_detector.apply import apply_penalty
from apollo.overseer.misconception_detector.types import MergeOutcome
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP
from apollo.resolution.nli_config import active_nli_model, nli_enabled
from apollo.resolution.result import ResolutionResult

_LOG = logging.getLogger(__name__)

# The resolution methods that can back a CREDITED ledger entry (spec §1's node
# ledger "resolution method" enum, widened to the full resolver tier set minus
# the two that can never win a covered/misconception finding: ``llm`` is the
# transcript-audit upgrade tier — tracked via ``AUDIT_UPGRADE_MESSAGE``, not the
# resolver — and ``unresolved`` is a confidence-0 non-match by definition).
CREDITED_METHODS: frozenset[str] = frozenset(
    m for m in METHOD_CONFIDENCE_CAP if m not in ("llm", "unresolved")
)

GRADER_USED_GRAPH = "graph"
GRADER_USED_LLM_FALLBACK = "llm_fallback"

_GRADER_VERSION_LLM_FALLBACK = "llm-fallback-v1"

# Lane B3a/D1 — the explicit "no misconceptions asserted (empty bank)" marker.
# Under the emergent-misconception design an empty ``apollo_misconceptions`` bank
# is the NORMAL cold-start state of every class: coverage grades normally and
# soundness is simply not assessed (``GradeResult.soundness_applicable=False``).
# This marker disambiguates an empty ``misconceptions: []`` list that was NEVER
# assessed (bank empty) from one that WAS assessed and found none — the machine-
# readable signal that replaced the removed ``misconception_bank_empty``
# abstention reason.
#
# It is nested UNDER the artifact's ``abstention`` block (key
# ``misconceptions_status``) — the one flexible-JSONB slot ``GradingArtifact``
# already persists (``models.py`` ``abstention`` column) that carries
# "what did/didn't grading assess" metadata alongside ``fallback_grade`` /
# ``graph_failure``. Nesting it there (rather than as a top-level payload key)
# is what makes it TRAVEL: ``artifact_writer._artifact_row`` maps ``abstention``
# to its column, so the marker reaches the persisted row AND the served
# scorecard (``render_scorecard`` reads ``abstention.misconceptions_status``).
# A top-level key would be silently dropped at persistence (no such column).
#
# Emitted on BOTH grader paths (``build_graph_artifact`` and
# ``build_llm_artifact``) but ONLY on the empty-bank branch, so a seeded-bank
# artifact stays byte-identical to today (no extra key in ``abstention``).
MISCONCEPTIONS_STATUS_KEY = "misconceptions_status"
MISCONCEPTIONS_STATUS_EMPTY_BANK = "empty_bank"


def _empty_bank_misconceptions_marker() -> dict:
    """The machine-readable "no misconceptions asserted (empty bank)" marker
    (lane B3a/D1) — nested in an artifact's ``abstention`` block (under
    ``misconceptions_status``) only when the misconception bank was empty/absent
    for the concept, on either grader path."""
    return {
        "assertable": False,
        "reason": MISCONCEPTIONS_STATUS_EMPTY_BANK,
        "detail": "no misconceptions asserted (empty bank)",
    }


def _method_lookup(resolution: ResolutionResult) -> dict[str, tuple[str, float]]:
    """``resolved_key -> (method, confidence)`` for every RESOLVED node (the
    only resolver outcome that can back a credited/misconception finding). A
    key resolved by more than one student node keeps the FIRST match — stable,
    deterministic (``resolution.resolved`` iterates in student-node order)."""
    out: dict[str, tuple[str, float]] = {}
    for rn in resolution.resolved:
        if rn.resolution != "resolved" or rn.resolved_key is None:
            continue
        out.setdefault(rn.resolved_key, (rn.method, rn.confidence))
    return out


def _evidence_span(finding: Finding) -> str:
    """Join a finding's evidence spans into one display string (empty when the
    finding carries none, e.g. a bare ``UNRESOLVED`` finding with no surface)."""
    return "; ".join(span for span in finding.evidence_spans if span)


def _node_ledger_entry(finding: Finding, methods: dict[str, tuple[str, float]]) -> dict:
    """One node-ledger row for a ``COVERED_NODE``/``CONTRADICTION`` finding."""
    key = finding.canonical_key
    method, resolved_confidence = methods.get(key, (None, None)) if key else (None, None)
    confidence = finding.confidence if finding.confidence is not None else resolved_confidence
    return {
        "canonical_key": key,
        "status": "misconception" if finding.kind == FindingKind.CONTRADICTION else "credited",
        "method": method,
        "confidence": confidence,
        "evidence_span": _evidence_span(finding),
    }


def _unresolved_ledger_entry(finding: Finding) -> dict:
    """One node-ledger row for an ``UNRESOLVED`` finding (a student utterance
    that matched no reference/misconception candidate)."""
    return {
        "canonical_key": finding.student_node_ids[0] if finding.student_node_ids else None,
        "status": "unresolved",
        "method": None,
        "confidence": 0.0,
        "evidence_span": _evidence_span(finding),
    }


def _missing_ledger_entry(finding: Finding) -> dict:
    """One node-ledger row for a ``MISSING_NODE`` finding (scorecard hardening,
    campaign-plan Task 3): a reference node the student's transcript never
    touched at all. Keyed by the REFERENCE node's own display-safe
    ``canonical_key`` (never a student-side id — there is no student evidence
    to key on). ``evidence_span`` and ``confidence`` are explicitly ``None``
    (no utterance was ever produced, so there is nothing to quote and no
    resolution was ever attempted) -- distinct from an ``UNRESOLVED`` row's
    ``evidence_span=""``/``confidence=0.0``, which record a REAL (failed)
    resolution attempt."""
    return {
        "canonical_key": finding.canonical_key,
        "status": "unresolved",
        "method": None,
        "confidence": None,
        "evidence_span": None,
    }


def build_node_ledger(findings: tuple[Finding, ...], resolution: ResolutionResult) -> list[dict]:
    """The full node ledger (spec §1): one row per ``credited``/``misconception``/
    ``unresolved`` finding, in ``findings`` order (already deterministic —
    ``GradeResult.findings`` is grouped-then-sorted, §6.4 step 8). ``unresolved``
    covers BOTH an audited ``UNRESOLVED`` student utterance and a ``MISSING_NODE``
    reference node the student never mentioned (Task 3 scorecard hardening) --
    the two are distinguished by ``canonical_key`` (student-side id vs. the
    reference node's own key) and by ``evidence_span``/``confidence`` (``""``/
    ``0.0`` for a real failed resolution vs. ``None`` for "never attempted")."""
    methods = _method_lookup(resolution)
    ledger: list[dict] = []
    for finding in findings:
        if finding.kind in (FindingKind.COVERED_NODE, FindingKind.CONTRADICTION):
            ledger.append(_node_ledger_entry(finding, methods))
        elif finding.kind == FindingKind.UNRESOLVED:
            ledger.append(_unresolved_ledger_entry(finding))
        elif finding.kind == FindingKind.MISSING_NODE:
            ledger.append(_missing_ledger_entry(finding))
    return ledger


def _parse_edge_message(message: str | None) -> dict:
    """Best-effort split of the diagnostic-only edge message
    (``"<from> -<TYPE>-> <to> (<provenance>)"``, see ``findings._edge_message``)
    into its parts; a malformed/missing message degrades to all-``None`` rather
    than raising (edges are diagnostic-only — never worth crashing artifact
    construction over)."""
    if not message:
        return {"from_key": None, "edge_type": None, "to_key": None, "provenance": None}
    try:
        left, rest = message.split(" -", 1)
        edge_type, rest = rest.split("-> ", 1)
        to_key, provenance = rest.rsplit(" (", 1)
        return {
            "from_key": left,
            "edge_type": edge_type,
            "to_key": to_key,
            "provenance": provenance.rstrip(")"),
        }
    except ValueError:
        return {"from_key": None, "edge_type": None, "to_key": None, "provenance": message}


def build_edge_ledger(findings: tuple[Finding, ...]) -> list[dict]:
    """The edge ledger (spec §1): one row per ``matched_edge``/``missing_edge``
    finding — same shape as the node ledger, coarser (edges carry no confidence
    or evidence span in v1; only the USES/PRECEDES relation itself)."""
    ledger: list[dict] = []
    for finding in findings:
        if finding.kind == FindingKind.MATCHED_EDGE:
            status = "matched"
        elif finding.kind == FindingKind.MISSING_EDGE:
            status = "missing"
        else:
            continue
        ledger.append({**_parse_edge_message(finding.message), "status": status})
    return ledger


def build_misconceptions(
    findings: tuple[Finding, ...],
    resolution: ResolutionResult,
    opposes_map: dict,
) -> list[dict]:
    """The misconceptions-asserted block (spec §1): each ``CONTRADICTION``
    finding with its triggering utterance + resolver confidence + the entity it
    opposes (``None`` when the candidate declared none)."""
    methods = _method_lookup(resolution)
    out: list[dict] = []
    for finding in findings:
        if finding.kind != FindingKind.CONTRADICTION:
            continue
        key = finding.canonical_key
        _, resolved_confidence = methods.get(key, (None, None)) if key else (None, None)
        confidence = finding.confidence if finding.confidence is not None else resolved_confidence
        out.append(
            {
                "canonical_key": key,
                "evidence_span": _evidence_span(finding),
                "confidence": confidence,
                "opposes": opposes_map.get(key) if key else None,
            }
        )
    return out


def _reference_node_count(findings: tuple[Finding, ...]) -> int:
    """The winning-path reference node count: every real reference node appears
    EXACTLY ONCE among ``COVERED_NODE``/``MISSING_NODE`` findings (§6.4 step 8 —
    each reference key on the winning path is either covered or missing, never
    both, never omitted)."""
    return sum(
        1 for f in findings if f.kind in (FindingKind.COVERED_NODE, FindingKind.MISSING_NODE)
    )


def compute_misconception_penalty(misconceptions: list[dict], reference_node_count: int) -> float:
    """``(count of asserted misconceptions with confidence >= MISC_CONFIDENCE_FLOOR)
    / max(1, reference_node_count)`` (Task A2 Step 4)."""
    asserted = sum(1 for m in misconceptions if (m["confidence"] or 0.0) >= MISC_CONFIDENCE_FLOOR)
    return asserted / max(1, reference_node_count)


def _versions_block(
    *, grader: str, reference_graph_hash: str | None, weights: CompositeWeights
) -> dict:
    return {
        "grader": grader,
        "reference_graph_hash": reference_graph_hash,
        "nli_model": active_nli_model() if nli_enabled() else None,
        "weights": {"w_n": weights.w_n, "w_e": weights.w_e, "p": weights.p},
    }


def build_graph_artifact(
    *,
    shadow: ShadowGradeResult,
    weights: CompositeWeights,
    clarification_trace: list[dict],
    latency_ms: int | None,
) -> dict:
    """Build the graph-grader artifact payload (spec §1) from an already-graded
    ``ShadowGradeResult``. Pure — reshapes ``shadow``'s frozen fields only.

    Lane B3a/D1: when the misconception bank was empty/absent
    (``shadow.grade.soundness_applicable is False``) coverage still grades
    normally and an explicit ``misconceptions_status`` marker is nested in the
    artifact's ``abstention`` block (no misconceptions were assessed) — the one
    persisted JSONB slot that reaches the row and the served scorecard. On the
    seeded path (the default) NO marker key is added, so the artifact is
    byte-identical."""
    findings = shadow.audited.findings
    node_ledger = build_node_ledger(findings, shadow.resolution)
    edge_ledger = build_edge_ledger(findings)
    misconceptions = build_misconceptions(findings, shadow.resolution, dict(shadow.opposes_map))

    node_coverage = shadow.grade.node_coverage_score
    edge_coverage = shadow.grade.edge_coverage_score
    misconception_penalty = compute_misconception_penalty(
        misconceptions, _reference_node_count(findings)
    )
    composite = composite_score(node_coverage, edge_coverage, misconception_penalty, weights)

    artifact = {
        "grader_used": GRADER_USED_GRAPH,
        "versions": _versions_block(
            grader=shadow.grade.comparison_version,
            reference_graph_hash=shadow.reference_graph_hash,
            weights=weights,
        ),
        "node_ledger": node_ledger,
        "edge_ledger": edge_ledger,
        "misconceptions": misconceptions,
        "clarification_trace": list(clarification_trace),
        "scores": {
            "node_coverage": node_coverage,
            "edge_coverage": edge_coverage,
            "misconception_penalty": misconception_penalty,
            "composite": composite,
            "weights": {"w_n": weights.w_n, "w_e": weights.w_e, "p": weights.p},
        },
        "abstention": {
            "abstained": shadow.audited.abstained,
            "reasons": list(shadow.audited.abstention_reasons),
            "normalization_confidence": shadow.normalization_confidence,
            "fallback_grade": None,
            "graph_failure": None,
        },
        "grading_latency_ms": latency_ms,
    }
    # Lane B3a/D1: empty bank -> coverage graded normally + explicit
    # "no misconceptions asserted (empty bank)" marker nested in the persisted
    # ``abstention`` block (so it travels to the row and the served scorecard).
    # Conditional so the seeded-bank artifact is byte-identical (no extra key).
    if not shadow.grade.soundness_applicable:
        artifact["abstention"][MISCONCEPTIONS_STATUS_KEY] = _empty_bank_misconceptions_marker()
    # §10 composite gate (APOLLO_ABSTENTION_COMPOSITE): nest the coverage/
    # contradictions/decision audit trail under ``abstention.composite`` only
    # when the flag was on for this attempt (``shadow.audited.composite`` is
    # None otherwise) — so a flag-OFF artifact stays byte-identical.
    if shadow.audited.composite is not None:
        artifact["abstention"]["composite"] = shadow.audited.composite
    return artifact


def _round_like_composite(value: float) -> float:
    """Clamp to ``[0, 1]`` and round to the same precision ``composite_score``
    uses, so an LLM-path composite compares equal to a graph-path one built
    from floating-point-equal inputs."""
    return round(max(0.0, min(1.0, value)), 6)


def build_llm_artifact(
    *,
    coverage: dict,
    rubric: dict,
    weights: CompositeWeights,
    graph_failure: str | None,
    latency_ms: int | None,
    clarification_trace: list[dict],
    misconceptions_bank_empty: bool = False,
    detection_outcome: MergeOutcome | None = None,
) -> dict:
    """Build the LLM-fallback artifact payload (spec §1/§3) from the OLD
    ``compute_coverage`` output + rubric. Coarser than the graph artifact:
    nodes come straight off ``coverage["per_step"]`` (the REAL
    ``compute_coverage`` return shape — a ``{node_id: "covered"|"missing"}``
    map, not a ``covered``/``missing`` list pair; no per-node resolution
    method/evidence — the LLM grader does not produce one), the edge ledger is
    always empty (the LLM path has no edge concept), and
    ``misconception_penalty`` is always 0.0 (the LLM path detects none).

    ``scores.composite`` is the headline number the student's scorecard bands
    off (spec §3 step 3: "Same scorecard shape either way — LLM grade
    rendered into the same band"). The LLM path has no edge/misconception
    decomposition to run through the graph path's weighted
    ``composite_score`` formula (that formula tops out at ``w_n`` when
    ``edge_coverage``/``misconception_penalty`` are both 0, which would
    silently cap every LLM-graded attempt below "Strong"). Instead the
    documented LLM-path mapping (spec §1/§3) is direct: the already-computed
    rubric's ``overall.score`` (0-100) IS the real LLM grade, so composite is
    that score renormalized to the artifact's 0-1 scale. ``node_coverage`` is
    still reported for informational/telemetry parity with the graph
    artifact's shape, but does not feed ``composite`` here.

    Lane B3a/D1: ``misconceptions_bank_empty`` threads the SAME empty-bank fact
    the graph path reads off ``shadow.grade.soundness_applicable`` (sourced from
    ``load_for_concept`` — see ``artifact_writer.write_artifacts``). When True,
    the ``misconceptions_status`` marker is nested in the ``abstention`` block
    exactly as on the graph path, so the SERVED scorecard (which templates over
    the LLM canonical payload whenever the graph grade was not promoted — the
    default in this build) can tell a cold-start empty bank apart from a checked
    "found none". Default False → the seeded/legacy path is byte-identical.

    A2/G2 fix (2026-07-03 campaign): ``clarification_trace`` is threaded
    through exactly like ``build_graph_artifact`` does — clarifications are
    SESSION-level evidence (the student's live answer-blind follow-up dialog),
    not grader-specific, so the LLM-fallback artifact must carry the same real
    trace the graph artifact gets, not a hardcoded ``[]``. Previously this
    builder ignored the caller's trace entirely, which meant the SERVED
    canonical artifact (this builder wins whenever ``served="llm_fallback"``,
    which is every attempt today — the graph grader is still shadow) always
    rendered an empty clarifications block on the student/teacher scorecard
    even when the live clarification loop ran and produced real exchanges.

    T11 (misconception detector wiring): ``detection_outcome`` is an OPT-IN
    keyword — ``None`` (the default) or an EMPTY ``MergeOutcome`` (zero
    penalty, no misconceptions) leaves ``misconception_penalty``,
    ``misconceptions``, and ``composite`` byte-identical to today (design
    invariant #1 — the detector's flag-OFF/found-nothing regression guard).
    A NON-empty outcome overrides ``misconception_penalty`` with
    ``outcome.misconception_penalty``, ``misconceptions`` with
    ``list(outcome.misconceptions)``, and recomputes ``composite`` via
    ``apply.apply_penalty`` on the already-computed (pre-penalty) composite —
    the detector only ever subtracts from or ceilings the LLM-path composite;
    it never touches ``node_coverage``/``edge_coverage`` or any other input.
    """
    per_step: dict[str, str] = coverage.get("per_step") or {}
    confidences: dict[str, float] = coverage.get("confidences") or {}
    covered = [key for key, status in per_step.items() if status == "covered"]
    missing = [key for key, status in per_step.items() if status != "covered"]
    total = len(per_step)
    node_coverage = (len(covered) / total) if total else 0.0
    edge_coverage = 0.0
    overall_score = (rubric or {}).get("overall", {}).get("score")
    composite = (
        _round_like_composite(float(overall_score) / 100.0) if overall_score is not None else 0.0
    )

    # T11: an outcome is only "active" when it carries something to apply —
    # None or an empty outcome (penalty 0.0, no rows) is the byte-identical
    # no-op path (design invariant #1).
    has_detection = detection_outcome is not None and not (
        detection_outcome.misconception_penalty == 0.0
        and not detection_outcome.misconceptions
        and not detection_outcome.ceiling_applied
    )
    if has_detection:
        misconception_penalty = detection_outcome.misconception_penalty
        misconceptions_rows = list(detection_outcome.misconceptions)
        composite = apply_penalty(composite=composite, outcome=detection_outcome)
    else:
        misconception_penalty = 0.0
        misconceptions_rows = []

    # Q2 fix (lane B4/2026-07-02 campaign): the LLM-fallback grader produces
    # per-node coverage (``per_step``) but NO per-node student utterance —
    # ``compute_coverage``'s ``per_step`` is a ``{ref_id: "covered"|"missing"}``
    # map with no surface text, and there is no deterministic node→step→
    # utterance mapping to recover one without an extra LLM call. So every row
    # carries ``evidence_span=None`` — the honest "no span available" value,
    # matching the established ``_missing_ledger_entry`` (MISSING_NODE)
    # convention: ``None`` = nothing to quote, distinct from a graph-path
    # ``UNRESOLVED`` utterance's ``""`` (a REAL failed resolution attempt with
    # an empty surface). NEVER ``""`` here — an empty-string span rendered as a
    # fake empty quote in the scorecard's "in the student's own words" block
    # (all 12 F1c packets) and, being non-null, was wrongly excluded from
    # ``classroom.struggle_signals`` (which keys reference-node 0.0-coverage
    # rows on ``evidence_span IS NULL``). The key stays PRESENT (value null) so
    # the S3 fidelity judge's ``.get('evidence_span')`` input shape is
    # unchanged (value goes ``"" -> null``, key presence untouched).
    node_ledger = [
        {
            "canonical_key": key,
            "status": "credited",
            "method": None,
            "confidence": confidences.get(key),
            "evidence_span": None,
        }
        for key in covered
    ] + [
        {
            "canonical_key": key,
            "status": "unresolved",
            "method": None,
            "confidence": confidences.get(key),
            "evidence_span": None,
        }
        for key in missing
    ]

    _LOG.debug(
        "build_llm_artifact node_ledger: %d credited, %d unresolved "
        "(evidence_span=None on the LLM path — no per-node student utterance)",
        len(covered),
        len(missing),
    )

    abstention: dict = {
        "abstained": None,
        "reasons": [],
        "normalization_confidence": None,
        "fallback_grade": overall_score,
        "graph_failure": graph_failure,
    }
    # Lane B3a/D1: empty bank -> nest the "no misconceptions asserted (empty
    # bank)" marker in the ``abstention`` block, identically to the graph path,
    # so the served scorecard can distinguish cold-start from checked-found-none.
    # Conditional so a seeded/legacy LLM artifact stays byte-identical (no key).
    if misconceptions_bank_empty:
        abstention[MISCONCEPTIONS_STATUS_KEY] = _empty_bank_misconceptions_marker()

    return {
        "grader_used": GRADER_USED_LLM_FALLBACK,
        "versions": _versions_block(
            grader=_GRADER_VERSION_LLM_FALLBACK, reference_graph_hash=None, weights=weights
        ),
        "node_ledger": node_ledger,
        "edge_ledger": [],
        "misconceptions": misconceptions_rows,
        "clarification_trace": list(clarification_trace),
        "scores": {
            "node_coverage": node_coverage,
            "edge_coverage": edge_coverage,
            "misconception_penalty": misconception_penalty,
            "composite": composite,
            "weights": {"w_n": weights.w_n, "w_e": weights.w_e, "p": weights.p},
            "llm_rubric": rubric,
        },
        "abstention": abstention,
        "grading_latency_ms": latency_ms,
    }
