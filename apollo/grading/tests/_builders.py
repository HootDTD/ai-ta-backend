"""WU-4B1 shared test builders.

Pure in-memory frozen fixtures — no resolver, no Neo4j, no LLM, no network.
Mirrors ``apollo/graph_compare/tests/_builders.py``: every helper returns a
frozen object, and the three deterministic ``audit_fn`` stubs (found / not-found
/ raising) let every audit path be exercised without a live API.
"""

from __future__ import annotations

from apollo.errors import TranscriptAuditUnavailableError
from apollo.grading.audited_grade import AuditedGrade
from apollo.grading.transcript_audit import AuditReply, AuditRequest
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.ontology.nodes import Node, NodeType, build_node
from apollo.resolution.candidates import Candidate
from apollo.resolution.result import ResolutionResult, ResolvedNode

# Minimal valid `content` payload per node_type — just enough to satisfy each
# discriminated-union content model's min_length fields in `build_node`.
_MIN_CONTENT_BY_TYPE: dict[NodeType, dict[str, str]] = {
    "equation": {"symbolic": "x"},
    "condition": {"applies_when": "x"},
    "simplification": {"applies_when": "x", "transformation": "y"},
    "definition": {"concept": "c", "meaning": "m"},
    "variable_mapping": {"term": "t", "symbol": "s"},
    "procedure_step": {"action": "a"},
}


def student_node(node_id: str, node_type: NodeType = "definition") -> Node:
    """A minimal student :class:`Node` carrying an explicit ``node_id`` +
    ``node_type``. The type-aware ``normalization_confidence`` reads each scored
    backing node's ``node_type`` (threaded as a ``node_id -> node_type`` map), so
    end-to-end ``build_audited_grade`` tests need real typed student nodes."""
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=-1,
        source="parser",
        content=_MIN_CONTENT_BY_TYPE[node_type],
    )


def missing_finding(key: str) -> Finding:
    """A ``missing_node`` finding for ``key`` (score 0.0, as core emits it)."""
    return Finding(kind=FindingKind.MISSING_NODE, canonical_key=key, score=0.0)


def contradiction_finding(key: str, *, student_node_ids: tuple[str, ...] = ()) -> Finding:
    """A contradiction (misconception) finding shaped EXACTLY as the real
    ``graph_compare.contradiction_finding`` factory emits it: ``confidence`` is
    None (that factory never sets it) and the evidence rides in
    ``student_node_ids``. The §6.6 misconception gate therefore sources its
    confidence from the resolution (keyed by these ids), NOT from this finding."""
    return Finding(
        kind=FindingKind.CONTRADICTION,
        canonical_key=key,
        student_node_ids=student_node_ids,
        score=0.0,
    )


def covered_finding(key: str, *, confidence: float = 0.92) -> Finding:
    return Finding(kind=FindingKind.COVERED_NODE, canonical_key=key, confidence=confidence)


def missing_grade(
    keys: tuple[str, ...] = (),
    *,
    contradictions: tuple[tuple[str, tuple[str, ...]], ...] = (),
    covered: tuple[str, ...] = (),
) -> GradeResult:
    """A :class:`GradeResult` with ``missing_node`` findings for ``keys`` (+
    optional contradiction / covered findings). All 10 score fields stubbed
    valid (non-NaN); ``comparison_confidence == 1.0`` (v1). Each contradiction is
    ``(misc_key, student_node_ids)`` — the §6.6 gate reads each id's confidence
    off the resolution (see :func:`resolution_with`'s ``resolved_nodes``)."""
    findings = (
        tuple(covered_finding(k) for k in covered)
        + tuple(missing_finding(k) for k in keys)
        + tuple(contradiction_finding(k, student_node_ids=nids) for k, nids in contradictions)
    )
    return GradeResult(
        coverage_score=0.5,
        soundness_score=1.0,
        bisimilarity_score=0.66,
        node_coverage_score=0.5,
        edge_coverage_score=0.5,
        scoping_score=1.0,
        usage_score=1.0,
        procedure_order_score=1.0,
        dependency_score=1.0,
        contradiction_score=1.0,
        comparison_confidence=1.0,
        findings=findings,
        comparison_version=COMPARISON_VERSION,
    )


def _resolved_node(node_id: str, resolution: str) -> ResolvedNode:
    return ResolvedNode(
        node_id=node_id,
        resolution=resolution,
        resolved_key=None if resolution != "resolved" else "k",
        resolved_canon_key=None,
        method="exact" if resolution == "resolved" else "unresolved",
        confidence=1.0 if resolution == "resolved" else 0.0,
    )


def _resolved_node_at(node_id: str, confidence: float) -> ResolvedNode:
    """A resolved node at an EXPLICIT confidence (the §6.6 misconception-gate
    input — the gate reads ``ResolvedNode.confidence`` by node_id)."""
    return ResolvedNode(
        node_id=node_id,
        resolution="resolved",
        resolved_key="k",
        resolved_canon_key=None,
        method="fuzzy",
        confidence=confidence,
    )


def resolution_with(
    *,
    unresolved: int = 0,
    resolved: int = 0,
    resolved_nodes: tuple[tuple[str, float], ...] = (),
) -> ResolutionResult:
    """A :class:`ResolutionResult` with the chosen tier mix (``resolved`` +
    ``unresolved`` nodes). ``unresolved_rate`` = unresolved / total. Pass
    ``resolved_nodes`` = ``((node_id, confidence), ...)`` to add explicitly-keyed
    resolved nodes at chosen confidences (so a contradiction finding's
    ``student_node_ids`` map onto a known misconception-resolution confidence)."""
    nodes = (
        tuple(_resolved_node(f"r{i}", "resolved") for i in range(resolved))
        + tuple(_resolved_node(f"u{i}", "unresolved") for i in range(unresolved))
        + tuple(_resolved_node_at(nid, conf) for nid, conf in resolved_nodes)
    )
    return ResolutionResult(resolved=nodes, tier_counts={}, llm_calls=0)


def nodes_with_confidences(*vals: float) -> tuple[Node, ...]:
    """``Node``s (definitions) at the given ``parser_confidence`` values."""
    return tuple(
        build_node(
            node_type="definition",
            node_id=f"n{i}",
            attempt_id=-1,
            source="parser",
            content={"concept": f"c{i}", "meaning": f"m{i}"},
            parser_confidence=v,
        )
        for i, v in enumerate(vals)
    )


def candidate(key: str, *, display_name: str | None = None, aliases=()) -> Candidate:
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=tuple(aliases),
        display_name=display_name if display_name is not None else key,
        opposes_key=None,
    )


def found_audit_fn(mapping: dict[str, str | None]):
    """A deterministic ``audit_fn`` that replies ``mapping`` for the asked keys.

    A key absent from ``mapping`` is reported ``None`` (not found). Records each
    request on ``.requests`` so a test can assert call count / per-chunk asks."""

    def _fn(request: AuditRequest) -> AuditReply:
        _fn.requests.append(request)  # type: ignore[attr-defined]
        return {e.canonical_key: mapping.get(e.canonical_key) for e in request.entities}

    _fn.requests = []  # type: ignore[attr-defined]
    return _fn


def notfound_audit_fn():
    """An ``audit_fn`` that replies ``None`` (not found) for every asked key."""

    def _fn(request: AuditRequest) -> AuditReply:
        _fn.requests.append(request)  # type: ignore[attr-defined]
        return {e.canonical_key: None for e in request.entities}

    _fn.requests = []  # type: ignore[attr-defined]
    return _fn


def raising_audit_fn():
    """An ``audit_fn`` that raises the NAMED infra error (audit unavailable)."""

    def _fn(request: AuditRequest) -> AuditReply:
        raise TranscriptAuditUnavailableError(last_error="boom")

    return _fn


# --- WU-4B2 finding->event builders -----------------------------------------


def covered_finding_with_nodes(
    key: str, nids: tuple[str, ...], *, confidence: float = 0.92
) -> Finding:
    """A ``covered_node`` finding carrying explicit ``student_node_ids`` (the
    §6.5 covered/conflict rows need the node ids to read turn positions)."""
    return Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key=key,
        student_node_ids=nids,
        confidence=confidence,
    )


def misc_candidate(key: str, opposes: str | None) -> Candidate:
    """A MISCONCEPTION ``Candidate`` whose ``opposes_key`` is ``opposes`` (None
    for the negative path that contributes nothing to the opposes map)."""
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="definition",
        is_misconception=True,
        symbolic=None,
        aliases=(),
        display_name=key,
        opposes_key=opposes,
    )


def audited(
    findings: tuple[Finding, ...],
    *,
    abstained: bool = False,
    suppressed: frozenset[str] | tuple[str, ...] = (),
) -> AuditedGrade:
    """A frozen :class:`AuditedGrade` LITERAL for the pure §6.5 table rows.

    ``grade`` carries a valid stub :class:`GradeResult` (4B2 never re-grades —
    it reads ``.findings``/``.abstained``/``.suppressed_event_kinds`` only).
    ``suppressed`` is coerced to a ``frozenset`` so a test may pass a tuple."""
    return AuditedGrade(
        grade=missing_grade(),
        findings=findings,
        abstention_reasons=(),
        abstained=abstained,
        suppressed_event_kinds=frozenset(suppressed),
        alias_candidates=(),
    )


def turn_order_of(**positions: int) -> dict[str, int]:
    """A ``turn_order`` mapping (node_id -> turn position) from kwargs, e.g.
    ``turn_order_of(c1=1, v1=2)``."""
    return dict(positions)
