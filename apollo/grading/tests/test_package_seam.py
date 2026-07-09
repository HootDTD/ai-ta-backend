"""WU-4B1 Step 0/§6.1 — public-API re-export + value-set parity tests.

The grading package is the downstream orchestration surface; this module pins
its public seam (every name importable from ``apollo.grading``), the RECON
correction (the audit cap is ``0.75 == llm tier``, NOT a key in the frozen
``METHOD_CONFIDENCE_CAP``), the abstention-threshold shape, and that the named
infra error is an :class:`ApolloError` now HTTP-registered (503) as of WU-4C1.
"""

from __future__ import annotations

import inspect

import apollo.api as apollo_api
from apollo.errors import ApolloError, TranscriptAuditUnavailableError
from apollo.graph_compare.findings import FindingKind
from apollo.persistence.models import FINDING_KINDS
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP


def test_public_api_exports():
    import apollo.grading as grading

    expected = {
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
    }
    assert expected.issubset(set(grading.__all__))
    for name in expected:
        assert hasattr(grading, name), f"apollo.grading is missing {name}"


def test_public_api_exports_wu4b2():
    """WU-4B2 GAINS the finding->event names; the existing 12 stay (backward-
    compat). New: convert_findings_to_events, LearnerEvent, LearnerEventKind,
    EVENT_CONVERSION_VERSION, build_opposes_map, PARTIAL_EDGE_GAP_ENABLED."""
    import apollo.grading as grading

    new_names = {
        "convert_findings_to_events",
        "LearnerEvent",
        "LearnerEventKind",
        "EVENT_CONVERSION_VERSION",
        "build_opposes_map",
        "PARTIAL_EDGE_GAP_ENABLED",
    }
    assert new_names.issubset(set(grading.__all__))
    for name in new_names:
        assert hasattr(grading, name), f"apollo.grading is missing {name}"


def test_finding_kind_unchanged():
    """grading imports the frozen FindingKind; it does NOT redefine it, and the
    value-set still mirrors models.FINDING_KINDS 1:1."""
    from apollo.grading.audited_grade import FindingKind as grading_finding_kind

    assert grading_finding_kind is FindingKind
    assert {k.value for k in FindingKind} == set(FINDING_KINDS)


def test_audit_cap_parity():
    """Locks the RECON correction: the audit cap equals the llm tier cap and
    'transcript_audit' is NOT a key added to the frozen METHOD_CONFIDENCE_CAP."""
    from apollo.grading import (
        TRANSCRIPT_AUDIT_CONFIDENCE_CAP,
        TRANSCRIPT_AUDIT_METHOD,
    )

    assert TRANSCRIPT_AUDIT_CONFIDENCE_CAP == 0.75
    assert TRANSCRIPT_AUDIT_CONFIDENCE_CAP == METHOD_CONFIDENCE_CAP["llm"]
    assert TRANSCRIPT_AUDIT_METHOD == "transcript_audit"
    assert "transcript_audit" not in METHOD_CONFIDENCE_CAP


def test_abstention_thresholds_shape():
    from apollo.grading import ABSTENTION_THRESHOLDS

    assert ABSTENTION_THRESHOLDS == {
        "unresolved_rate": 0.35,
        "min_parser_confidence": 0.6,
        "misconception_confidence": 0.8,
        "min_normalization_confidence": 0.85,  # Phase 1c quality brake (calibration-owned, spec §10)
    }


def test_transcript_audit_error_is_apollo_error_and_http_registered():
    """The named infra error is an ApolloError (NO-FALLBACK registry) and, as of
    WU-4C1, IS wired into apollo/api.py's exception handlers (503 — the shadow
    Done chain re-raises it; this WU-4B1 forward-guard flipped from `not in`)."""
    assert issubclass(TranscriptAuditUnavailableError, ApolloError)

    src = inspect.getsource(apollo_api.register_exception_handlers)
    assert "TranscriptAuditUnavailableError" in src
