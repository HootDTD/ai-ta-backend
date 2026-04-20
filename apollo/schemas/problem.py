"""Pydantic schema for a problem file with structured reference solution.

A problem is: a text statement, given values, target unknown, and an
ordered list of KG entries (equation | definition | condition |
simplification | variable_mapping) that must be present in the student's
KG for the solver to reach the target.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


EntryType = Literal[
    "equation", "definition", "condition", "simplification",
    "variable_mapping", "procedure_step"
]
Difficulty = Literal["intro", "standard", "hard"]


class ReferenceStep(BaseModel):
    step: int = Field(ge=1)
    entry_type: EntryType
    id: str = Field(min_length=1)
    content: Dict[str, Any]
    depends_on: List[str] = Field(default_factory=list)


class Problem(BaseModel):
    id: str = Field(min_length=1)
    concept_id: str = Field(min_length=1)
    difficulty: Difficulty
    problem_text: str = Field(min_length=1)
    given_values: Dict[str, float]
    target_unknown: str = Field(min_length=1)
    reference_solution: List[ReferenceStep] = Field(min_length=1)


def load_problem(path: str | Path) -> Problem:
    """Load and validate a problem JSON file."""
    text = Path(path).read_text()
    return Problem.model_validate_json(text)
