# apollo/textbook_ingest/concept_schema_map.py
"""Pure mapping between ConceptRegistryEntry / ConceptDefinition and the Neo4j
concept subgraph row shapes. See the plan's Spec Reconciliations items 5/6/7.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from apollo.subjects import (
    CanonicalSymbols, ConceptDefinition, ForbiddenNamedLaws, SolverHints,
)
from apollo.textbook_ingest.types import ConceptRegistryEntry

# These two dicts are exact inverses; preserve the bijection if adding a category.
_FORBIDDEN_CATEGORIES = {
    "named_law": "named_laws",
    "forbidden_concept": "forbidden_concepts",
    "forbidden_domain": "forbidden_domains",
    "forbidden_unit": "forbidden_units",
}
_FORBIDDEN_FIELD_TO_CATEGORY = {
    "named_laws": "named_law",
    "forbidden_concepts": "forbidden_concept",
    "forbidden_domains": "forbidden_domain",
    "forbidden_units": "forbidden_unit",
}

# kind discriminator for SolverConstant rows: SolverHints field -> stored kind
_SOLVER_KINDS = {"constants": "constant", "augmented_givens": "augmented_given"}


def entry_to_rows(entry: ConceptRegistryEntry) -> dict[str, Any]:
    cs = entry.canonical_symbols
    symbols = [
        {"symbol": s, "description": cs.description.get(s, ""),
         "subscript_convention": cs.subscript_convention or "", "ord": i}
        for i, s in enumerate(cs.symbols)
    ]
    normalization = [
        {"natural_language": nl, "canonical_symbol": sym}
        for nl, sym in entry.normalization_map.items()
    ]
    constants = (
        [{"name": n, "value": v, "kind": _SOLVER_KINDS["constants"]}
         for n, v in entry.solver_hints.constants.items()]
        + [{"name": n, "value": v, "kind": _SOLVER_KINDS["augmented_givens"]}
           for n, v in entry.solver_hints.augmented_givens.items()]
    )
    forbidden = entry.forbidden_named_laws
    forbidden_terms = []
    for field, category in _FORBIDDEN_FIELD_TO_CATEGORY.items():
        for term in getattr(forbidden, field):
            forbidden_terms.append({"term": term, "category": category})
    return {
        "concept": {
            "subject_id": entry.subject_id, "concept_id": entry.concept_id,
            "scope_summary": entry.scope_summary,
            "parser_prompt_template": entry.parser_prompt_template,
            "non_trivial_keywords": list(entry.solver_hints.non_trivial_keywords),
            "plan_markers": list(entry.solver_hints.plan_markers),
        },
        "symbols": symbols,
        "normalization": normalization,
        "solver_constants": constants,
        "forbidden_terms": forbidden_terms,
    }


def rows_to_concept_definition(rows: dict[str, Any],
                               problems_dir: Path | None) -> ConceptDefinition:
    concept = rows["concept"]
    symbols = [r["symbol"] for r in rows["symbols"]]
    description = {r["symbol"]: r["description"] for r in rows["symbols"] if r["description"]}
    subscript = next((r["subscript_convention"] for r in rows["symbols"]
                      if r["subscript_convention"]), None)
    normalization = {r["natural_language"]: r["canonical_symbol"] for r in rows["normalization"]}
    constants = {r["name"]: r["value"] for r in rows["solver_constants"]
                 if r["kind"] == _SOLVER_KINDS["constants"]}
    augmented = {r["name"]: r["value"] for r in rows["solver_constants"]
                 if r["kind"] == _SOLVER_KINDS["augmented_givens"]}
    forbidden_lists: dict[str, list[str]] = {f: [] for f in _FORBIDDEN_CATEGORIES.values()}
    for t in rows["forbidden_terms"]:
        forbidden_lists[_FORBIDDEN_CATEGORIES[t["category"]]].append(t["term"])
    return ConceptDefinition(
        subject_id=concept["subject_id"], concept_id=concept["concept_id"],
        canonical_symbols=CanonicalSymbols(symbols=symbols, description=description,
                                           subscript_convention=subscript),
        normalization_map=normalization,
        parser_prompt_template=concept["parser_prompt_template"],
        solver_hints=SolverHints(
            constants=constants, augmented_givens=augmented,
            non_trivial_keywords=list(concept.get("non_trivial_keywords", [])),
            plan_markers=list(concept.get("plan_markers", [])),
        ),
        forbidden_named_laws=ForbiddenNamedLaws(**forbidden_lists),
        problems_dir=problems_dir or Path("/nonexistent"),
    )
