"""GEN-0 hard invariant for problem and reference-solution provenance."""

from __future__ import annotations

import pytest

from apollo.persistence.models import ConceptProblem


@pytest.mark.parametrize(
    ("write_path", "provenance", "solution_source"),
    [
        ("scrape.write_tier1_problems", {"document_id": 1}, None),
        ("ingest.write_authored_tier1_problems", {"source": "authored"}, "authored"),
        ("promote (generated default)", {"source": "generated"}, "generated"),
        ("promote (paired document)", {"document_id": 1}, "extracted"),
    ],
)
def test_solution_source_write_paths_respect_provenance(write_path, provenance, solution_source):
    """Enumerate every production stamper and pin its permitted provenance pair."""
    problem = ConceptProblem(
        concept_id=1,
        problem_code=write_path,
        difficulty="intro",
        payload={},
        tier=1,
        provenance=provenance,
        solution_source=solution_source,
    )
    assert problem.solution_source == solution_source


@pytest.mark.parametrize("forbidden_source", ["extracted", "authored", "llm_paired"])
def test_machine_generated_problem_rejects_teacher_grounded_solution_source(
    forbidden_source,
):
    """Machine generation can never masquerade as extracted/authored truth."""
    with pytest.raises(ValueError, match="machine-generated problem provenance"):
        ConceptProblem(
            concept_id=1,
            problem_code=f"generated.{forbidden_source}",
            difficulty="intro",
            payload={},
            tier=1,
            provenance={"source": "generated"},
            solution_source=forbidden_source,
        )


def test_machine_generated_provenance_cannot_be_applied_after_source_stamp():
    """The guard is assignment-order independent for promotion/update paths."""
    problem = ConceptProblem(
        concept_id=1,
        problem_code="generated.late-provenance",
        difficulty="intro",
        payload={},
        tier=1,
        solution_source="extracted",
        provenance={"source": "document"},
    )

    with pytest.raises(ValueError, match="machine-generated problem provenance"):
        problem.provenance = {"source": "generated"}
