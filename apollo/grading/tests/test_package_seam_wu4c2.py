"""WU-4C2 — public-API re-export parity (additive over WU-4B1/4B2/4B3).

Every new WU-4C2 public name is importable off ``apollo.grading`` and present in
``__all__``; the prior names survive (backward-compat).
"""

from __future__ import annotations

_WU4C2_NAMES = {
    # calibration (§6.7)
    "CALIBRATION_VERSION",
    "AxisDelta",
    "CalibrationMetrics",
    "compute_calibration_metrics",
    # rubric mapping (§6.4)
    "RubricMappingInput",
    "RubricRefNode",
    "build_graph_sim_rubric",
    "findings_to_rubric_input",
    # constrained diagnostic (§6.8)
    "ConstrainedDiagnostic",
    "DiagnosticFinding",
    "DiagnosticRequest",
    "generate_constrained_diagnostic",
    "main_chat_diagnostic_llm",
}

# A representative sample of the WU-4B names that must SURVIVE.
_PRIOR_NAMES = {
    "build_audited_grade",
    "AuditedGrade",
    "convert_findings_to_events",
    "build_opposes_map",
    "persist_comparison_run",
    "compute_normalization_confidence",
    "reference_graph_hash",
}


def test_public_api_exports_wu4c2():
    import apollo.grading as grading

    assert _WU4C2_NAMES.issubset(set(grading.__all__))
    for name in _WU4C2_NAMES:
        assert hasattr(grading, name), f"apollo.grading is missing {name}"


def test_prior_names_still_exported():
    import apollo.grading as grading

    assert _PRIOR_NAMES.issubset(set(grading.__all__))
    for name in _PRIOR_NAMES:
        assert hasattr(grading, name), f"apollo.grading dropped {name}"
