# apollo/textbook_ingest/tests/test_validator.py
from pathlib import Path

from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, SolverHints, ConceptDefinition
from apollo.textbook_ingest.types import ExtractedProblem
from apollo.textbook_ingest.validator import validate_problem, ValidationResult


def _concept():
    return ConceptDefinition(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        canonical_symbols=CanonicalSymbols(symbols=["P1", "P2"]), normalization_map={},
        parser_prompt_template="P", solver_hints=SolverHints(),
        problems_dir=Path("/nonexistent"))


def _good():
    return ExtractedProblem(
        source_document_id="d", source_chunk_id="c", source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics", difficulty="intro")


def test_gate1_passes_valid_payload():
    res = validate_problem(_good(), _concept())
    assert isinstance(res, ValidationResult)
    assert res.ok is True
    assert res.gate_failed is None


def test_gate1_fails_invalid_node_content():
    bad = _good()
    bad.reference_solution[0].content = {"label": "x"}  # missing required 'symbolic'
    res = validate_problem(bad, _concept())
    assert res.ok is False
    assert res.gate_failed == "schema"
