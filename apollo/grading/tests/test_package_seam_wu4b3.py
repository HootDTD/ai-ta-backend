"""WU-4B3 — public-API re-export parity (additive over WU-4B1/4B2).

The grading package GAINS the persistence + normalization_confidence +
reference_hash names; the existing WU-4B1/4B2 names stay (backward-compat). Also
pins that every ``models.FINDING_KINDS`` value is producible as a
``FindingRowSpec.finding_kind`` (no kind silently unmappable).
"""

from __future__ import annotations

from apollo.grading.persistence import finding_to_row_spec
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.persistence.models import FINDING_KINDS

_WU4B3_NAMES = {
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
}

# The WU-4B1/4B2 names that must SURVIVE (backward-compat).
_PRIOR_NAMES = {
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
    "convert_findings_to_events",
    "LearnerEvent",
    "LearnerEventKind",
    "EVENT_CONVERSION_VERSION",
    "build_opposes_map",
    "PARTIAL_EDGE_GAP_ENABLED",
}


def test_public_api_exports_wu4b3():
    import apollo.grading as grading

    assert _WU4B3_NAMES.issubset(set(grading.__all__))
    for name in _WU4B3_NAMES:
        assert hasattr(grading, name), f"apollo.grading is missing {name}"


def test_prior_names_still_exported():
    """Backward-compat: the WU-4B1/4B2 names stay in __all__ + on the module."""
    import apollo.grading as grading

    assert _PRIOR_NAMES.issubset(set(grading.__all__))
    for name in _PRIOR_NAMES:
        assert hasattr(grading, name), f"apollo.grading dropped {name}"


def test_finding_row_spec_kinds_cover_models_finding_kinds():
    """Every value in models.FINDING_KINDS is producible as a FindingRowSpec
    finding_kind (no §2 kind silently unmappable)."""
    produced = set()
    for kind in FindingKind:
        spec = finding_to_row_spec(Finding(kind=kind, canonical_key="k"))
        produced.add(spec.finding_kind)
    assert produced == set(FINDING_KINDS)
