"""WU-4A1 Task 8 — the public package seam.

Mirrors ``apollo/resolution/__init__.py``: every public name is re-exported from
``apollo.graph_compare`` with a flat ``__all__``. These pin that the seam is
complete (no stray export, no missing name) so WU-4A2 imports the package, not
the individual modules.
"""

from __future__ import annotations

import apollo.graph_compare as gc

_EXPECTED = {
    # canonical.py
    "CanonicalNode",
    "CanonicalEdge",
    "CanonicalGraph",
    "ReferencePathView",
    "ReferenceGraph",
    "build_student_canonical",
    "build_reference_canonical",
    # validator.py
    "validate_student_graph",
    "validate_reference",
    "StudentGraphInvalidError",
    "ReferenceGraphInvalidError",
    # problem_inputs.py
    "ProblemInputs",
    "build_problem_candidates",
    # core.py (WU-4A2)
    "grade_attempt",
    "GradeResult",
    "COMPARISON_VERSION",
    # findings.py (WU-4A2)
    "Finding",
    "FindingKind",
}


def test_public_api_importable_from_package():
    """Every name in __all__ imports directly from apollo.graph_compare."""
    for name in gc.__all__:
        assert hasattr(gc, name), f"{name} not importable from apollo.graph_compare"


def test_all_matches_exports():
    """__all__ equals the sorted expected public set (no stray, no missing)."""
    assert sorted(gc.__all__) == sorted(_EXPECTED)


def test_each_export_is_the_module_object():
    """The re-exported names are the SAME objects as the module-level defs (a
    re-export, not a shadow)."""
    from apollo.graph_compare import canonical, core, findings, problem_inputs, validator

    assert gc.CanonicalNode is canonical.CanonicalNode
    assert gc.build_student_canonical is canonical.build_student_canonical
    assert gc.build_reference_canonical is canonical.build_reference_canonical
    assert gc.validate_student_graph is validator.validate_student_graph
    assert gc.validate_reference is validator.validate_reference
    assert gc.StudentGraphInvalidError is validator.StudentGraphInvalidError
    assert gc.ReferenceGraphInvalidError is validator.ReferenceGraphInvalidError
    assert gc.ProblemInputs is problem_inputs.ProblemInputs
    assert gc.build_problem_candidates is problem_inputs.build_problem_candidates
    # WU-4A2 — re-exports are the SAME objects as the module-level defs.
    assert gc.grade_attempt is core.grade_attempt
    assert gc.GradeResult is core.GradeResult
    assert gc.COMPARISON_VERSION is core.COMPARISON_VERSION
    assert gc.Finding is findings.Finding
    assert gc.FindingKind is findings.FindingKind
