"""WU-4B3 §6.4 step 15 — runs+findings Postgres persistence (supersede).

The PERSISTENCE seam of the §6 grading orchestration. It maps the frozen
``GradeResult`` (score authority) + ``AuditedGrade`` (abstention + audit-rewritten
findings authority) onto the ``apollo_graph_comparison_runs`` /
``apollo_graph_comparison_findings`` tables (ORM shipped by migration 026), then
writes them in ONE transaction with SUPERSEDE semantics.

Mirrors ``apollo/knowledge_graph/resolution_store.py``: PURE ``*_to_row`` spec
dataclasses (DB-free, 1:1 column mapping, testable without a container) + a thin
async write seam that the test harness drives on real pgvector.

Binding decisions:
  * Persist ``audited.findings`` — the audit-REWRITTEN set. An audit-upgraded
    missing->covered must persist as the COVERED it became (carrying the span +
    capped confidence + ``AUDIT_UPGRADE_MESSAGE``); persisting ``grade.findings``
    (the PRE-audit set) would lose that upgrade.
  * SUPERSEDE: a re-run at the same ``(attempt_id, comparison_version)`` DELETEs
    the prior run (its findings CASCADE) then reinserts — a legit retry must NEVER
    crash on ``UNIQUE(attempt_id, comparison_version)``. DELETE+INSERT share one
    transaction (no commit between), so the supersede is atomic.
  * Persist ALWAYS, including abstained runs (``abstained=true``, findings still
    written) — NO early return on abstention (§6.4 step 15 persist-always).
  * Does NOT ``commit()`` — the caller (WU-4C ``done.py``) owns the transaction
    boundary. We ``flush()`` so the ``run_id`` is real + FK-valid within the open
    transaction.

``entity_id`` is NULL in v1: a ``Finding`` carries the ``canonical_key`` STRING,
not the ``apollo_kg_entities.id`` surrogate; the key->id join is not a WU-4B3
concern (the ``ON DELETE SET NULL`` FK tolerates NULL, the canonical_key survives
in the ids/message for diagnostics). ``student_edge_ids`` / ``reference_edge_ids``
are always ``[]`` — edges are message-only diagnostics (frozen ``findings.py``).

Pure mapping + immutable: builds NEW spec objects, never mutates inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.core import GradeResult
from apollo.graph_compare.findings import Finding
from apollo.persistence.models import GraphComparisonFinding, GraphComparisonRun


@dataclass(frozen=True)
class RunRowSpec:
    """A pure pre-DB value object mapping 1:1 onto ``apollo_graph_comparison_runs``
    columns (NO ``id`` / ``created_at`` — the DB owns those). Immutable."""

    attempt_id: int
    user_id: str
    search_space_id: int
    coverage_score: float  # top-line 3 are NOT NULL (§2 schema)
    soundness_score: float
    bisimilarity_score: float
    node_coverage_score: float | None  # the 7 sub-scores are nullable (§2 schema)
    edge_coverage_score: float | None
    scoping_score: float | None
    usage_score: float | None
    procedure_order_score: float | None
    dependency_score: float | None
    contradiction_score: float | None
    normalization_confidence: float  # NOT NULL (§2 schema)
    abstained: bool
    abstention_reasons: tuple[str, ...]
    comparison_version: str
    reference_graph_hash: str


@dataclass(frozen=True)
class FindingRowSpec:
    """A pure pre-DB value object mapping 1:1 onto
    ``apollo_graph_comparison_findings`` columns (NO ``id`` / ``run_id`` /
    ``created_at``). JSONB id/span fields are PLAIN LISTS (the ``_JSONType``
    column stores arrays). Immutable."""

    finding_kind: str
    entity_id: int | None
    score: float | None
    confidence: float | None
    student_node_ids: list[str]
    reference_node_ids: list[str]
    student_edge_ids: list[str]
    reference_edge_ids: list[str]
    evidence_spans: list[str]
    message: str | None


def grade_to_run_spec(
    *,
    attempt_id: int,
    user_id: str,
    search_space_id: int,
    grade: GradeResult,
    audited: AuditedGrade,
    normalization_confidence: float,
    reference_graph_hash: str,
) -> RunRowSpec:
    """Map a ``GradeResult`` (scores) + ``AuditedGrade`` (abstention) onto a
    ``RunRowSpec``. The 10 ``*_score`` fields copy 1:1 from ``grade``;
    ``abstained`` / ``abstention_reasons`` come from ``audited`` (NOT recomputed);
    the two scalars + ``comparison_version`` (off ``grade``) land verbatim."""
    return RunRowSpec(
        attempt_id=attempt_id,
        user_id=user_id,
        search_space_id=search_space_id,
        coverage_score=grade.coverage_score,
        soundness_score=grade.soundness_score,
        bisimilarity_score=grade.bisimilarity_score,
        node_coverage_score=grade.node_coverage_score,
        edge_coverage_score=grade.edge_coverage_score,
        scoping_score=grade.scoping_score,
        usage_score=grade.usage_score,
        procedure_order_score=grade.procedure_order_score,
        dependency_score=grade.dependency_score,
        contradiction_score=grade.contradiction_score,
        normalization_confidence=normalization_confidence,
        abstained=audited.abstained,
        abstention_reasons=audited.abstention_reasons,
        comparison_version=grade.comparison_version,
        reference_graph_hash=reference_graph_hash,
    )


def finding_to_row_spec(finding: Finding) -> FindingRowSpec:
    """Map one in-memory ``Finding`` onto a ``FindingRowSpec``.

    ``finding_kind`` is the StrEnum ``.value`` plain string; node-id / span tuples
    become lists for the JSONB column; edge ids are always ``[]`` (edges are
    message-only); ``entity_id`` is NULL in v1 (the canonical_key->id join is not
    a WU-4B3 concern)."""
    return FindingRowSpec(
        finding_kind=finding.kind.value,
        entity_id=None,
        score=finding.score,
        confidence=finding.confidence,
        student_node_ids=list(finding.student_node_ids),
        reference_node_ids=list(finding.reference_node_ids),
        student_edge_ids=[],
        reference_edge_ids=[],
        evidence_spans=list(finding.evidence_spans),
        message=finding.message,
    )


def findings_to_row_specs(findings: tuple[Finding, ...]) -> tuple[FindingRowSpec, ...]:
    """Map a findings tuple onto a tuple of ``FindingRowSpec`` (order preserved)."""
    return tuple(finding_to_row_spec(f) for f in findings)


def _run_orm_from_spec(spec: RunRowSpec) -> GraphComparisonRun:
    """Build the ``GraphComparisonRun`` ORM row from a ``RunRowSpec`` (abstention
    reasons list-ified for the JSONB column)."""
    return GraphComparisonRun(
        attempt_id=spec.attempt_id,
        user_id=spec.user_id,
        search_space_id=spec.search_space_id,
        coverage_score=spec.coverage_score,
        soundness_score=spec.soundness_score,
        bisimilarity_score=spec.bisimilarity_score,
        node_coverage_score=spec.node_coverage_score,
        edge_coverage_score=spec.edge_coverage_score,
        scoping_score=spec.scoping_score,
        usage_score=spec.usage_score,
        procedure_order_score=spec.procedure_order_score,
        dependency_score=spec.dependency_score,
        contradiction_score=spec.contradiction_score,
        normalization_confidence=spec.normalization_confidence,
        abstained=spec.abstained,
        abstention_reasons=list(spec.abstention_reasons),
        comparison_version=spec.comparison_version,
        reference_graph_hash=spec.reference_graph_hash,
    )


def _finding_orm_from_spec(spec: FindingRowSpec, *, run_id: int) -> GraphComparisonFinding:
    return GraphComparisonFinding(
        run_id=run_id,
        entity_id=spec.entity_id,
        finding_kind=spec.finding_kind,
        score=spec.score,
        confidence=spec.confidence,
        student_node_ids=spec.student_node_ids,
        reference_node_ids=spec.reference_node_ids,
        student_edge_ids=spec.student_edge_ids,
        reference_edge_ids=spec.reference_edge_ids,
        evidence_spans=spec.evidence_spans,
        message=spec.message,
    )


async def persist_comparison_run(
    db: AsyncSession,
    *,
    attempt_id: int,
    user_id: str,
    search_space_id: int,
    grade: GradeResult,
    audited: AuditedGrade,
    normalization_confidence: float,
    reference_graph_hash: str,
) -> int:
    """Persist the comparison run + one findings row per ``audited.findings``
    Finding, with SUPERSEDE, in the caller's transaction. Returns the run_id.

    1. Build the pure ``RunRowSpec`` + ``FindingRowSpec``s.
    2. DELETE the prior run at ``(attempt_id, comparison_version)`` (findings
       CASCADE) — the §2 supersede; a legit retry never hits the UNIQUE crash.
    3. INSERT the new run; ``flush()`` to materialize the ``run_id``.
    4. Bulk-INSERT the findings with ``run_id`` set.
    Does NOT ``commit()`` — the caller owns the txn boundary. Persists ALWAYS,
    including abstained runs."""
    run_spec = grade_to_run_spec(
        attempt_id=attempt_id,
        user_id=user_id,
        search_space_id=search_space_id,
        grade=grade,
        audited=audited,
        normalization_confidence=normalization_confidence,
        reference_graph_hash=reference_graph_hash,
    )
    finding_specs = findings_to_row_specs(audited.findings)

    # Supersede: remove any prior run for this (attempt, version); its findings
    # CASCADE-drop. Same transaction as the insert below -> atomic.
    await db.execute(
        delete(GraphComparisonRun).where(
            GraphComparisonRun.attempt_id == attempt_id,
            GraphComparisonRun.comparison_version == run_spec.comparison_version,
        )
    )

    run = _run_orm_from_spec(run_spec)
    db.add(run)
    await db.flush()  # materialize run.id within the open transaction

    for spec in finding_specs:
        db.add(_finding_orm_from_spec(spec, run_id=run.id))
    await db.flush()

    return run.id
