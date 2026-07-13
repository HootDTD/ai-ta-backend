"""Variation operators for teacher-initiated problem generation.

Each operator owns a stable, module-level prompt and a content-derived
applicability predicate. Quantitative-only operators deactivate for prose seeds;
the general operators preserve qualitative targets without inventing numbers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from apollo.schemas.problem import Problem

PARAMETER_PERTURBATION_PROMPT = """\
Create a variant with the SAME problem structure and context but NEW numeric
values. Use plausible magnitudes, avoid degenerate values such as 0 or 1 when
they trivialize the problem, and update the statement so every number remains
consistent with given_values.
"""

CONTEXT_RESKIN_PROMPT = """\
Create a variant with the SAME underlying problem structure but a NEW surface
context or scenario. For a quantitative seed, retain or lightly perturb its
values. For a prose seed, preserve the conceptual target while changing the
scenario; do not invent numeric givens.
"""

ISOMORPHIC_DAG_SHAPE_PROMPT = """\
Create a NEW problem with a different context and different specifics whose
solution follows the SAME dependency shape as the structure-only skeleton
provided below. Do not reconstruct or repeat content from the seed solution.
"""

SHARED_OUTPUT_CONTRACT = """\
Return one JSON object with exactly these fields:
- problem_text: a non-empty standalone problem statement
- given_values: an object mapping symbols to numeric values; use {} for prose
- target_unknown: the symbolic unknown or qualitative target phrase
- difficulty: exactly the seed difficulty shown below

NEVER include the solution, final answer, or worked steps in problem_text.
For prose problems, do not force equations or numeric values.
Return the JSON object ONLY.
"""


def _always(_seed: Problem) -> bool:
    return True


def _has_given_values(seed: Problem) -> bool:
    return bool(seed.given_values)


def _dag_skeleton(seed: Problem) -> list[dict[str, object]]:
    """Return structure only: no equations, labels, or other content fields."""
    return [
        {"entry_type": step.entry_type, "depends_on": list(step.depends_on)}
        for step in seed.reference_solution
    ]


@dataclass(frozen=True)
class VariationOperator:
    name: str
    prompt: str
    applicable: Callable[[Problem], bool]
    include_dag_skeleton: bool = False

    def build_messages(self, seed_payload: Problem) -> list[dict]:
        seed: dict[str, object] = {
            "problem_text": seed_payload.problem_text,
            "given_values": seed_payload.given_values,
            "target_unknown": seed_payload.target_unknown,
            "difficulty": seed_payload.difficulty,
        }
        if self.include_dag_skeleton:
            seed["dependency_shape"] = _dag_skeleton(seed_payload)
        return [
            {
                "role": "system",
                "content": f"{self.prompt.strip()}\n\n{SHARED_OUTPUT_CONTRACT.strip()}",
            },
            {
                "role": "user",
                "content": json.dumps(seed, sort_keys=True, separators=(",", ":")),
            },
        ]


VARIATION_OPERATORS: tuple[VariationOperator, ...] = (
    VariationOperator(
        name="parameter_perturbation",
        prompt=PARAMETER_PERTURBATION_PROMPT,
        applicable=_has_given_values,
    ),
    VariationOperator(
        name="context_reskin",
        prompt=CONTEXT_RESKIN_PROMPT,
        applicable=_always,
    ),
    VariationOperator(
        name="isomorphic_dag_shape",
        prompt=ISOMORPHIC_DAG_SHAPE_PROMPT,
        applicable=_always,
        include_dag_skeleton=True,
    ),
)

__all__ = [
    "CONTEXT_RESKIN_PROMPT",
    "ISOMORPHIC_DAG_SHAPE_PROMPT",
    "PARAMETER_PERTURBATION_PROMPT",
    "SHARED_OUTPUT_CONTRACT",
    "VARIATION_OPERATORS",
    "VariationOperator",
]
