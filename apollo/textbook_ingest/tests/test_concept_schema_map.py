# apollo/textbook_ingest/tests/test_concept_schema_map.py
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest.concept_schema_map import (
    entry_to_rows, rows_to_concept_definition,
)
from apollo.textbook_ingest.types import ConceptRegistryEntry


def _entry():
    return ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="Bernoulli for incompressible steady flow.",
        canonical_symbols=CanonicalSymbols(symbols=["P", "rho"],
                                           description={"P": "pressure", "rho": "density"},
                                           subscript_convention="P1/P2 ..."),
        normalization_map={"pressure": "P", "density": "rho"},
        parser_prompt_template="PROMPT",
        solver_hints=SolverHints(constants={"g": 9.81}, augmented_givens={"g": 9.81},
                                 non_trivial_keywords=["pressure"], plan_markers=["first"]),
        forbidden_named_laws=ForbiddenNamedLaws(named_laws=["bernoulli"],
                                                forbidden_concepts=["viscosity"],
                                                forbidden_domains=["physics"],
                                                forbidden_units=["pascals"]),
    )


def test_entry_to_rows_categorizes_forbidden_and_constants():
    rows = entry_to_rows(_entry())
    cats = {(t["term"], t["category"]) for t in rows["forbidden_terms"]}
    assert ("bernoulli", "named_law") in cats
    assert ("viscosity", "forbidden_concept") in cats
    assert ("physics", "forbidden_domain") in cats
    assert ("pascals", "forbidden_unit") in cats
    kinds = {(c["name"], c["kind"]) for c in rows["solver_constants"]}
    assert ("g", "constant") in kinds and ("g", "augmented_given") in kinds
    assert rows["concept"]["non_trivial_keywords"] == ["pressure"]
    assert rows["concept"]["plan_markers"] == ["first"]


def test_round_trips_to_concept_definition():
    rows = entry_to_rows(_entry())
    cdef = rows_to_concept_definition(rows, problems_dir=None)
    assert cdef.canonical_symbols.symbols == ["P", "rho"]
    assert cdef.normalization_map["pressure"] == "P"
    assert cdef.solver_hints.augmented_givens == {"g": 9.81}
    assert cdef.solver_hints.non_trivial_keywords == ["pressure"]
    assert "bernoulli" in cdef.forbidden_named_laws.named_laws
    assert "viscosity" in cdef.forbidden_named_laws.forbidden_concepts
