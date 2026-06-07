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
