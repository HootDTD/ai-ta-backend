from apollo.overseer.rubric import (
    AXIS_WEIGHTS,
    LETTER_BANDS,
    compute_rubric,
    score_to_letter,
)


def test_letter_bands_cover_0_to_100():
    # Every integer 0..100 maps to some letter.
    letters = {score_to_letter(s) for s in range(0, 101)}
    assert letters == {"A+", "A", "A-", "B+", "B", "B-", "C+", "C", "D", "F"}


def test_score_to_letter_boundaries():
    assert score_to_letter(100) == "A+"
    assert score_to_letter(97) == "A+"
    assert score_to_letter(96) == "A"
    assert score_to_letter(90) == "A"
    assert score_to_letter(89) == "A-"
    assert score_to_letter(85) == "A-"
    assert score_to_letter(84) == "B+"
    assert score_to_letter(80) == "B+"
    assert score_to_letter(49) == "F"
    assert score_to_letter(0) == "F"


def test_compute_rubric_all_axes_full_coverage():
    refs = [
        {"id": "eq1", "entry_type": "equation", "content": {"label": "x"}, "step": 1, "depends_on": []},
        {"id": "c1", "entry_type": "condition", "content": {"label": "x"}, "step": 2, "depends_on": []},
        {"id": "s1", "entry_type": "simplification", "content": {"applies_when": "x"}, "step": 3, "depends_on": []},
        {"id": "p1", "entry_type": "procedure_step", "content": {"order": 1, "action": "x", "uses_equations": [], "purpose": "y"}, "step": 4, "depends_on": []},
    ]
    coverage = {
        "per_step": {"eq1": "covered", "c1": "covered", "s1": "covered", "p1": "covered"},
        "procedure_scores": {"p1": 1.0},
    }
    rubric = compute_rubric(coverage, refs)
    assert rubric["overall"]["score"] == 100
    assert rubric["overall"]["letter"] == "A+"
    assert rubric["procedure"]["score"] == 100
    assert rubric["justification"]["score"] == 100
    assert rubric["simplification"]["score"] == 100
    assert "variables" not in rubric


def test_compute_rubric_procedure_only_failure():
    refs = [
        {"id": "eq1", "entry_type": "equation", "content": {"label": "x"}, "step": 1, "depends_on": []},
        {"id": "c1", "entry_type": "condition", "content": {"label": "x"}, "step": 2, "depends_on": []},
        {"id": "p1", "entry_type": "procedure_step", "content": {"order": 1, "action": "x", "uses_equations": [], "purpose": "y"}, "step": 3, "depends_on": []},
    ]
    # Student covered everything except procedure.
    coverage = {
        "per_step": {"eq1": "covered", "c1": "covered", "p1": "missing"},
        "procedure_scores": {"p1": 0.0},
    }
    rubric = compute_rubric(coverage, refs)
    # With simplification axis absent, weights redistribute:
    # Procedure = 0.60, Justification = 0.25; total = 0.85 -> rescale to 1.0.
    # Proc 0.0 * (0.60/0.85) + Just 1.0 * (0.25/0.85) = 29.41...
    assert rubric["overall"]["score"] == 29
    assert rubric["procedure"]["score"] == 0
    assert rubric["justification"]["score"] == 100


def test_compute_rubric_partial_procedure_credit():
    refs = [
        {"id": "p1", "entry_type": "procedure_step", "content": {"order": 1, "action": "x", "uses_equations": [], "purpose": "y"}, "step": 1, "depends_on": []},
        {"id": "p2", "entry_type": "procedure_step", "content": {"order": 2, "action": "x", "uses_equations": [], "purpose": "y"}, "step": 2, "depends_on": []},
    ]
    coverage = {
        "per_step": {"p1": "covered", "p2": "missing"},
        "procedure_scores": {"p1": 0.9, "p2": 0.4},
    }
    rubric = compute_rubric(coverage, refs)
    # Only procedure axis present -> all weight on procedure.
    # Procedure mean = (0.9 + 0.4) / 2 = 0.65 -> 65.
    assert rubric["procedure"]["score"] == 65
    assert rubric["overall"]["score"] == 65


def test_compute_rubric_empty_reference_is_zero():
    rubric = compute_rubric({"per_step": {}, "procedure_scores": {}}, [])
    # No axes present — overall degenerates to 0 (the student had nothing to teach).
    assert rubric["overall"]["score"] == 0
    assert rubric["overall"]["letter"] == "F"


def test_axis_weights_sum_to_one():
    assert abs(sum(AXIS_WEIGHTS.values()) - 1.0) < 1e-9


def test_axis_weights_procedure_dominates():
    assert AXIS_WEIGHTS["procedure"] == 0.60
    assert AXIS_WEIGHTS["justification"] == 0.25
    assert AXIS_WEIGHTS["simplification"] == 0.15
    assert "variables" not in AXIS_WEIGHTS


def test_letter_bands_structure():
    # LETTER_BANDS is a list of (min_score, letter) tuples in descending order.
    assert LETTER_BANDS[0] == (97, "A+")
    assert LETTER_BANDS[-1] == (0, "F")


def test_compute_rubric_ignores_definition_and_variable_mapping():
    # Definition and variable_mapping are no longer graded axes.
    refs = [
        {"id": "d1", "entry_type": "definition", "content": {"concept": "pressure"}, "step": 1, "depends_on": []},
        {"id": "v1", "entry_type": "variable_mapping", "content": {"term": "rho"}, "step": 2, "depends_on": []},
    ]
    coverage = {"per_step": {}, "procedure_scores": {}}
    rubric = compute_rubric(coverage, refs)
    # No axes present — overall degenerates to 0, and variables key is gone.
    assert "variables" not in rubric
    assert rubric["procedure"]["present"] is False
    assert rubric["justification"]["present"] is False
    assert rubric["simplification"]["present"] is False
    assert rubric["overall"]["score"] == 0


def test_compute_rubric_coerces_nan_score_to_zero():
    import math as _m
    refs = [
        {"id": "p1", "entry_type": "procedure_step", "content": {"order": 1, "action": "x", "uses_equations": [], "purpose": "y"}, "step": 1, "depends_on": []},
    ]
    coverage = {"per_step": {"p1": "covered"}, "procedure_scores": {"p1": float("nan")}}
    rubric = compute_rubric(coverage, refs)
    # NaN is coerced to 0; the axis score should be 0, not crash.
    assert rubric["procedure"]["score"] == 0
    assert rubric["overall"]["score"] == 0
