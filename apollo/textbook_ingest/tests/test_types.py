from apollo.schemas.problem import ReferenceStep
from apollo.textbook_ingest.types import (
    ConceptCandidate, ConceptResolution, ConceptRegistryEntry,
    ProblemCandidate, ExtractedProblem, ValidatedProblem, RejectedProblem,
)


def _ref_step():
    return ReferenceStep(step=1, entry_type="equation", id="eq1",
                         content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
                         depends_on=[])


def test_extracted_to_validated_inherits():
    ex = ExtractedProblem(
        source_document_id="d", source_chunk_id="c", source_page=3,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[_ref_step()], concept_id="bernoulli_principle",
        subject_id="fluid_mechanics", difficulty="intro",
    )
    vp = ValidatedProblem(**ex.model_dump(), problem_id="abc123")
    assert vp.problem_id == "abc123"
    assert vp.subject_id == "fluid_mechanics"


def test_resolution_new_has_no_match():
    cand = ConceptCandidate(proposed_subject_id="s", proposed_concept_id="c",
                            scope_summary="x", source_chunk_ids=["a"], confidence=0.9)
    res = ConceptResolution(kind="new", candidate=cand, matched_concept_id=None,
                            matched_subject_id=None, resolution_method=None,
                            similarity_score=None)
    assert res.kind == "new"


def test_remaining_types_construct():
    from apollo.subjects import CanonicalSymbols, SolverHints
    cand = ConceptCandidate(proposed_subject_id="s", proposed_concept_id="c",
                            scope_summary="x", source_chunk_ids=["a"], confidence=0.9)
    entry = ConceptRegistryEntry(subject_id="s", concept_id="c", scope_summary="x",
        canonical_symbols=CanonicalSymbols(symbols=["P"]), normalization_map={},
        parser_prompt_template="t", solver_hints=SolverHints())
    pc = ProblemCandidate(source_document_id="d", source_chunk_id="c", source_page=1,
                          raw_text="x", detected_kind="exercise", confidence=0.8)
    rej = RejectedProblem(extracted=ExtractedProblem(
        source_document_id="d", source_chunk_id="c", source_page=1, problem_text="t",
        given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]}, depends_on=[])],
        concept_id="c", subject_id="s", difficulty="intro"),
        gate_failed="schema", gate_diagnostic="x")
    assert entry.concept_id == "c" and pc.detected_kind == "exercise" and rej.gate_failed == "schema"
