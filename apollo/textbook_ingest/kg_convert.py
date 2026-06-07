"""Convert a list[ReferenceStep] into the ontology KGGraph used by validator
gates and the Neo4j writer. Reuses Problem.to_kg_graph so reference-solution
graph derivation stays single-sourced.
"""
from __future__ import annotations

from apollo.ontology.graph import KGGraph
from apollo.schemas.problem import Problem, ReferenceStep

# Reference subgraphs are global, not per-attempt. Fixed negative sentinel keeps
# them clear of any real (positive) student attempt_id and of test ids.
REFERENCE_ATTEMPT_ID = -1


def reference_steps_to_kg_graph(steps: list[ReferenceStep]) -> KGGraph:
    shell = Problem(
        id="__ref__", concept_id="__ref__", difficulty="intro",
        problem_text="__ref__", given_values={}, target_unknown="__ref__",
        reference_solution=steps,
    )
    return shell.to_kg_graph(attempt_id=REFERENCE_ATTEMPT_ID)
