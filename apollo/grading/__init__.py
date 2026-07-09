"""WU-4B1 — the §6 grading ORCHESTRATION layer (transcript audit + abstention).

Downstream of the pure, IO-free ``apollo/graph_compare/`` score core, which it
IMPORTS (never extends): ``graph_compare`` persists nothing and runs no
Neo4j/Postgres/LLM, so the Done-time transcript audit (ONE batched LLM call),
the §6.6 abstention gates, and the :class:`AuditedGrade` handoff assembly live
HERE — exactly mirroring the ``apollo/resolution/`` (matching) ->
``apollo/knowledge_graph/resolution_store`` (writes) split.

This package persists NOTHING (runs/findings + ``abstention_reasons``/
``abstained`` writes are WU-4B3), produces NO events (finding->event conversion
is WU-4B2), and emits :class:`AliasCandidate` value objects only (the §8
teacher-approval queue is WU-3B2). The transcript is passed in as text (WU-4C
threads the ``apollo_messages`` read).
"""

from __future__ import annotations

from apollo.errors import TranscriptAuditUnavailableError
from apollo.grading.abstention import (
    ABSTENTION_THRESHOLDS,
    Abstention,
    apply_abstention,
)
from apollo.grading.audited_grade import AuditedGrade, build_audited_grade
from apollo.grading.event_model import (
    EVENT_CONVERSION_VERSION,
    LearnerEvent,
    LearnerEventKind,
)
from apollo.grading.events import (
    PARTIAL_EDGE_GAP_ENABLED,
    convert_findings_to_events,
)
from apollo.grading.normalization_confidence import (
    NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES,
    compute_normalization_confidence,
)
from apollo.grading.opposes import build_opposes_map
from apollo.grading.persistence import (
    FindingRowSpec,
    RunRowSpec,
    finding_to_row_spec,
    findings_to_row_specs,
    grade_to_run_spec,
    persist_comparison_run,
)
from apollo.grading.reference_hash import (
    REFERENCE_HASH_VERSION,
    reference_graph_hash,
)
from apollo.grading.transcript_audit import (
    TRANSCRIPT_AUDIT_CONFIDENCE_CAP,
    TRANSCRIPT_AUDIT_METHOD,
    AliasCandidate,
    AuditResult,
    MissingEntity,
    audit_missing,
)

# WU-4C2 — calibration harness (§6.7) + rubric mapping (§6.4) + constrained
# diagnostic (§6.8). Additive over WU-4B1/4B2/4B3.
from apollo.grading.calibration import (
    CALIBRATION_VERSION,
    AxisDelta,
    CalibrationMetrics,
    compute_calibration_metrics,
)
from apollo.grading.diagnostic import (
    ConstrainedDiagnostic,
    DiagnosticFinding,
    DiagnosticRequest,
    generate_constrained_diagnostic,
    main_chat_diagnostic_llm,
)
from apollo.grading.rubric_mapping import (
    RubricMappingInput,
    RubricRefNode,
    build_graph_sim_rubric,
    findings_to_rubric_input,
)

__all__ = [
    "audit_missing",
    "AuditResult",
    "MissingEntity",
    "AliasCandidate",
    "TranscriptAuditUnavailableError",
    "TRANSCRIPT_AUDIT_CONFIDENCE_CAP",
    "TRANSCRIPT_AUDIT_METHOD",
    "apply_abstention",
    "Abstention",
    "ABSTENTION_THRESHOLDS",
    "build_audited_grade",
    "AuditedGrade",
    # WU-4B2 — finding->event conversion (§6.5 decision table)
    "convert_findings_to_events",
    "LearnerEvent",
    "LearnerEventKind",
    "EVENT_CONVERSION_VERSION",
    "build_opposes_map",
    "PARTIAL_EDGE_GAP_ENABLED",
    # WU-4B3 — runs/findings persistence (supersede) + the §3 damper input +
    # the reference-graph fingerprint.
    "persist_comparison_run",
    "RunRowSpec",
    "FindingRowSpec",
    "grade_to_run_spec",
    "finding_to_row_spec",
    "findings_to_row_specs",
    "compute_normalization_confidence",
    "NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES",
    "reference_graph_hash",
    "REFERENCE_HASH_VERSION",
    # WU-4C2 — calibration (§6.7) + rubric mapping (§6.4) + constrained
    # diagnostic (§6.8).
    "CALIBRATION_VERSION",
    "AxisDelta",
    "CalibrationMetrics",
    "compute_calibration_metrics",
    "RubricMappingInput",
    "RubricRefNode",
    "build_graph_sim_rubric",
    "findings_to_rubric_input",
    "ConstrainedDiagnostic",
    "DiagnosticFinding",
    "DiagnosticRequest",
    "generate_constrained_diagnostic",
    "main_chat_diagnostic_llm",
]
