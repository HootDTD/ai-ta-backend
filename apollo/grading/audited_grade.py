"""WU-4B1 §6.4 step 12 + step 14 — assemble the :class:`AuditedGrade` handoff.

:func:`build_audited_grade` is the WU-4B1 orchestrator. It imports the pure
WU-4A2 score core's :class:`GradeResult` and turns it into a frozen
:class:`AuditedGrade`:

1. Collect ``missing_node`` findings -> :class:`MissingEntity` list (display
   name + aliases looked up from the closed candidate set; a key absent from
   ``candidates`` falls back to its own key as display name).
2. Run :func:`audit_missing`. On :class:`TranscriptAuditUnavailableError` the
   error is CAUGHT here (the audit boundary) — ``transcript_audit_failed`` is
   set and the audit result is empty (NO upgrades, NO alias candidates). The
   error is NEVER re-raised past this boundary; it converts to the suppress-ALL-
   ``missing`` abstention reason (§6.6 binding: an audit-infra failure suppresses
   every ``missing``, NEVER emits one).
3. Compute the §6.6 gate inputs and call :func:`apply_abstention`.
4. Rewrite the findings: an audit-found ``missing_node`` is REPLACED by a NEW
   covered-grade finding at ``confidence <= 0.75`` carrying the span (immutable —
   a new tuple, the frozen input findings are never mutated).
5. Return the frozen :class:`AuditedGrade`.

Persists NOTHING (runs/findings + ``abstention_reasons``/``abstained`` writes
are WU-4B3); produces NO events (finding->event is WU-4B2); emits
:class:`AliasCandidate` value objects only (the §8 teacher-approval queue is
WU-3B2).
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.errors import TranscriptAuditUnavailableError
from apollo.grading.abstention import (
    apply_abstention,
    min_parser_confidence_of,
    unresolved_rate_of,
)
from apollo.grading.transcript_audit import (
    TRANSCRIPT_AUDIT_CONFIDENCE_CAP,
    TRANSCRIPT_AUDIT_METHOD,
    AliasCandidate,
    AuditFn,
    AuditResult,
    MissingEntity,
    audit_missing,
)
from apollo.graph_compare.core import GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.result import ResolutionResult

# The marker WU-4B2's decision table reads off an upgraded finding (Finding has
# no `method` field — frozen, WU-4A2 — so the audit provenance rides in
# `message` + the capped `confidence` + the quoted `evidence_spans`).
AUDIT_UPGRADE_MESSAGE = f"upgraded by {TRANSCRIPT_AUDIT_METHOD}"


@dataclass(frozen=True)
class AuditedGrade:
    """The frozen WU-4B1 handoff artifact: a :class:`GradeResult` with
    audit-upgraded findings + the abstention outcome + emitted alias candidates.

    WU-4B2 reads ``.findings`` + ``.suppressed_event_kinds`` to convert findings
    -> events (§6.5); WU-4B3 persists ``.abstention_reasons`` + ``.abstained`` on
    the runs row. The score math is carried UNCHANGED in ``.grade`` (WU-4B1 never
    re-grades)."""

    grade: GradeResult
    findings: tuple[Finding, ...]
    abstention_reasons: tuple[str, ...]
    abstained: bool
    suppressed_event_kinds: frozenset[str]
    alias_candidates: tuple[AliasCandidate, ...]


def _missing_entities(
    findings: tuple[Finding, ...], candidates: tuple[Candidate, ...]
) -> tuple[MissingEntity, ...]:
    """One :class:`MissingEntity` per ``missing_node`` finding, display name +
    aliases looked up from the closed candidate set (fallback: the key itself)."""
    by_key = {c.canonical_key: c for c in candidates}
    out: list[MissingEntity] = []
    for finding in findings:
        if finding.kind != FindingKind.MISSING_NODE or finding.canonical_key is None:
            continue
        cand = by_key.get(finding.canonical_key)
        out.append(
            MissingEntity(
                canonical_key=finding.canonical_key,
                display_name=cand.display_name if cand else finding.canonical_key,
                aliases=cand.aliases if cand else (),
            )
        )
    return tuple(out)


def _upgraded_finding(canonical_key: str, span: str) -> Finding:
    """A NEW covered-grade finding replacing an audit-found ``missing_node``.

    Carries the audit provenance in ``message`` + the capped ``confidence`` +
    the quoted ``evidence_spans`` (Finding has no ``method`` field). WU-4B2 reads
    ``confidence <= 0.75`` + the ``transcript_audit`` message marker to grade it
    ``partial``/``covered``."""
    return Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key=canonical_key,
        evidence_spans=(span,),
        confidence=TRANSCRIPT_AUDIT_CONFIDENCE_CAP,
        message=AUDIT_UPGRADE_MESSAGE,
    )


def _rewrite_findings(findings: tuple[Finding, ...], audit: AuditResult) -> tuple[Finding, ...]:
    """Replace each audit-found ``missing_node`` with its upgraded covered
    finding; leave every other finding untouched. Immutable: a NEW tuple, the
    frozen input findings are never mutated."""
    out: list[Finding] = []
    for finding in findings:
        if (
            finding.kind == FindingKind.MISSING_NODE
            and finding.canonical_key in audit.upgraded_keys
        ):
            out.append(
                _upgraded_finding(finding.canonical_key, audit.spans_by_key[finding.canonical_key])
            )
        else:
            out.append(finding)
    return tuple(out)


def _misconception_confidences(findings: tuple[Finding, ...]) -> tuple[float, ...]:
    """The confidences of the contradiction findings (the misconception gate
    input). A contradiction with no confidence is treated as ``1.0`` (the §6.6
    gate only withholds on a LOW confidence; a None confidence never withholds)."""
    return tuple(
        f.confidence if f.confidence is not None else 1.0
        for f in findings
        if f.kind == FindingKind.CONTRADICTION
    )


def build_audited_grade(
    grade: GradeResult,
    *,
    transcript: str,
    resolution: ResolutionResult,
    student_nodes: tuple[Node, ...],
    candidates: tuple[Candidate, ...] = (),
    reference_invalid: bool = False,
    audit_fn: AuditFn | None = None,
) -> AuditedGrade:
    """Orchestrate §6.4 step 12 + step 14 into the frozen :class:`AuditedGrade`.

    ``audit_fn`` defaults to the live :func:`main_chat_auditor`; every test
    injects a deterministic stub (CI-safe, no live LLM)."""
    missing = _missing_entities(grade.findings, candidates)

    transcript_audit_failed = False
    try:
        audit = audit_missing(missing, transcript, audit_fn=audit_fn)
    except TranscriptAuditUnavailableError:
        # §6.6 binding: an audit-infra failure suppresses ALL missing, NEVER
        # emits one. Caught HERE (not re-raised), surfaced as the abstention
        # reason below — proof it was handled, not silently swallowed.
        transcript_audit_failed = True
        audit = AuditResult(upgraded_keys=frozenset(), spans_by_key={}, alias_candidates=())

    abstention = apply_abstention(
        unresolved_rate=unresolved_rate_of(resolution),
        min_parser_confidence=min_parser_confidence_of(student_nodes),
        misconception_confidences=_misconception_confidences(grade.findings),
        transcript_audit_failed=transcript_audit_failed,
        reference_invalid=reference_invalid,
    )

    new_findings = _rewrite_findings(grade.findings, audit)

    return AuditedGrade(
        grade=grade,
        findings=new_findings,
        abstention_reasons=abstention.abstention_reasons,
        abstained=abstention.abstained,
        suppressed_event_kinds=abstention.suppressed_event_kinds,
        alias_candidates=audit.alias_candidates,
    )
