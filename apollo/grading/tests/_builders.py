"""WU-4B1 shared test builders.

Pure in-memory frozen fixtures — no resolver, no Neo4j, no LLM, no network.
Mirrors ``apollo/graph_compare/tests/_builders.py``: every helper returns a
frozen object, and the three deterministic ``audit_fn`` stubs (found / not-found
/ raising) let every audit path be exercised without a live API.
"""

from __future__ import annotations

from apollo.errors import TranscriptAuditUnavailableError
from apollo.grading.transcript_audit import AuditReply, AuditRequest
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.ontology.nodes import Node, build_node
from apollo.resolution.candidates import Candidate
from apollo.resolution.result import ResolutionResult, ResolvedNode


def missing_finding(key: str) -> Finding:
    """A ``missing_node`` finding for ``key`` (score 0.0, as core emits it)."""
    return Finding(kind=FindingKind.MISSING_NODE, canonical_key=key, score=0.0)


def contradiction_finding(key: str, *, confidence: float) -> Finding:
    """A contradiction (misconception) finding at the given confidence."""
    return Finding(
        kind=FindingKind.CONTRADICTION,
        canonical_key=key,
        confidence=confidence,
        score=0.0,
    )


def covered_finding(key: str, *, confidence: float = 0.92) -> Finding:
    return Finding(kind=FindingKind.COVERED_NODE, canonical_key=key, confidence=confidence)


def missing_grade(
    keys: tuple[str, ...] = (),
    *,
    contradictions: tuple[tuple[str, float], ...] = (),
    covered: tuple[str, ...] = (),
) -> GradeResult:
    """A :class:`GradeResult` with ``missing_node`` findings for ``keys`` (+
    optional contradiction / covered findings). All 10 score fields stubbed
    valid (non-NaN); ``comparison_confidence == 1.0`` (v1)."""
    findings = (
        tuple(covered_finding(k) for k in covered)
        + tuple(missing_finding(k) for k in keys)
        + tuple(contradiction_finding(k, confidence=c) for k, c in contradictions)
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


def resolution_with(*, unresolved: int = 0, resolved: int = 0) -> ResolutionResult:
    """A :class:`ResolutionResult` with the chosen tier mix (``resolved`` +
    ``unresolved`` nodes). ``unresolved_rate`` = unresolved / (unresolved +
    resolved)."""
    nodes = tuple(_resolved_node(f"r{i}", "resolved") for i in range(resolved)) + tuple(
        _resolved_node(f"u{i}", "unresolved") for i in range(unresolved)
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
