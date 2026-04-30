"""Apollo V3 subject + concept registry.

Layout:
    apollo/subjects/<subject_id>/concepts/<concept_id>/
        canonical_symbols.json    # symbol vocabulary (e.g. P, rho, v, A, h, g, Q)
        normalization_map.json    # natural-language term -> canonical symbol
        parser_prompt_template.md # Jinja-style template; receives canonical_symbols
        solver_hints.json         # constants + per-concept augmentations
        concept_dag.json          # concept prerequisite DAG (optional)
        problems/                 # problem_*.json bank for this concept

`load_concept(subject_id, concept_id)` returns a `ConceptDefinition`. All
hardcoded fluid-mechanics knowledge in the parser / solver / done handler
should be replaced with reads from this registry.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


_SUBJECTS_ROOT = Path(__file__).resolve().parent


class ConceptNotFoundError(LookupError):
    """Raised when a subject/concept pair has no registry entry."""


class CanonicalSymbols(BaseModel):
    symbols: list[str]
    description: dict[str, str] = Field(default_factory=dict)
    subscript_convention: str | None = None


class SolverHints(BaseModel):
    constants: dict[str, float] = Field(default_factory=dict)
    augmented_givens: dict[str, float] = Field(default_factory=dict)
    non_trivial_keywords: list[str] = Field(default_factory=list)
    plan_markers: list[str] = Field(default_factory=list)


class ForbiddenNamedLaws(BaseModel):
    """Concept-scoped pre-filter list for the deterministic stage of the
    output filter (item #3). The LLM-judge stage handles paraphrases.

    All four lists are unioned at filter time; the structure exists for
    authoring clarity, not for differential semantics.
    """

    named_laws: list[str] = Field(default_factory=list)
    forbidden_concepts: list[str] = Field(default_factory=list)
    forbidden_domains: list[str] = Field(default_factory=list)
    forbidden_units: list[str] = Field(default_factory=list)

    def all_terms(self) -> list[str]:
        return [
            t.lower()
            for t in (
                *self.named_laws,
                *self.forbidden_concepts,
                *self.forbidden_domains,
                *self.forbidden_units,
            )
        ]


class ConceptDefinition(BaseModel):
    """All on-disk registry data for a (subject, concept)."""

    subject_id: str
    concept_id: str
    canonical_symbols: CanonicalSymbols
    normalization_map: dict[str, str]
    parser_prompt_template: str
    solver_hints: SolverHints
    forbidden_named_laws: ForbiddenNamedLaws = Field(
        default_factory=ForbiddenNamedLaws,
    )
    problems_dir: Path

    model_config = {"arbitrary_types_allowed": True}


def _concept_dir(subject_id: str, concept_id: str) -> Path:
    return _SUBJECTS_ROOT / subject_id / "concepts" / concept_id


def list_subjects() -> list[str]:
    return sorted(
        p.name for p in _SUBJECTS_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith("_") and not p.name.startswith(".")
    )


def list_concepts(subject_id: str) -> list[str]:
    concepts_dir = _SUBJECTS_ROOT / subject_id / "concepts"
    if not concepts_dir.is_dir():
        return []
    return sorted(
        p.name for p in concepts_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def load_concept(subject_id: str, concept_id: str) -> ConceptDefinition:
    cdir = _concept_dir(subject_id, concept_id)
    if not cdir.is_dir():
        raise ConceptNotFoundError(f"{subject_id}/{concept_id} not in registry")

    sym_path = cdir / "canonical_symbols.json"
    norm_path = cdir / "normalization_map.json"
    prompt_path = cdir / "parser_prompt_template.md"
    hints_path = cdir / "solver_hints.json"
    forbidden_path = cdir / "forbidden_named_laws.json"
    problems_dir = cdir / "problems"

    forbidden = (
        ForbiddenNamedLaws.model_validate_json(forbidden_path.read_text())
        if forbidden_path.is_file()
        else ForbiddenNamedLaws()
    )

    return ConceptDefinition(
        subject_id=subject_id,
        concept_id=concept_id,
        canonical_symbols=CanonicalSymbols.model_validate_json(sym_path.read_text()),
        normalization_map=json.loads(norm_path.read_text()),
        parser_prompt_template=prompt_path.read_text(),
        solver_hints=SolverHints.model_validate_json(hints_path.read_text()),
        forbidden_named_laws=forbidden,
        problems_dir=problems_dir,
    )


__all__ = [
    "ConceptDefinition",
    "ConceptNotFoundError",
    "CanonicalSymbols",
    "SolverHints",
    "ForbiddenNamedLaws",
    "list_subjects",
    "list_concepts",
    "load_concept",
]
