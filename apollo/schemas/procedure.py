"""ProcedureStep: a single ordered step in a student's plan to solve a problem.

A procedure step answers 'what do I do at this stage of the solution?'
It references which equations it uses (by label or id) and states its
purpose. Unlike equations, procedure steps are free-form natural language
and are graded by the coverage matcher with a 0-1 partial-credit score."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProcedureStep(BaseModel):
    order: int = Field(ge=1)
    action: str = Field(min_length=1)
    uses_equations: list[str] = Field(default_factory=list)
    purpose: str = Field(min_length=1)
