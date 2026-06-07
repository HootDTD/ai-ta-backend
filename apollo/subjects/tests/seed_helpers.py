"""Shared test helpers for seeding concept policy into Neo4j.

Replaces the former filesystem `load_concept("fluid_mechanics",
"bernoulli_principle")` fixture used by the output-filter and leakage-judge
tests. The forbidden-term lists mirror the original on-disk
`forbidden_named_laws.json` so the deterministic pre-filter behaviour is
preserved exactly.
"""
from __future__ import annotations

from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints, load_concept
from apollo.textbook_ingest import writer
from apollo.textbook_ingest.types import ConceptRegistryEntry

BERNOULLI_FORBIDDEN = ForbiddenNamedLaws(
    named_laws=["bernoulli", "continuity", "navier", "stokes", "pascal", "torricelli"],
    forbidden_concepts=[
        "viscosity", "viscous", "compressible", "compressibility",
        "incompressible", "incompressibility", "turbulence", "turbulent",
        "laminar", "streamline", "streamlines", "kinetic", "potential",
        "enthalpy", "entropy", "conservation",
    ],
    forbidden_domains=[
        "physics", "mechanics", "hydrodynamics", "aerodynamics", "thermodynamics",
    ],
    forbidden_units=["pascals", "newton", "newtons", "joule", "joules"],
)


async def seed_bernoulli_concept(neo):
    """Write the bernoulli_principle concept policy subgraph and return the
    reconstructed ConceptDefinition."""
    await writer.write_concept(
        neo,
        ConceptRegistryEntry(
            subject_id="fluid_mechanics",
            concept_id="bernoulli_principle",
            scope_summary="Bernoulli principle for incompressible flow.",
            canonical_symbols=CanonicalSymbols(
                symbols=["P", "rho", "v", "A", "h", "g", "Q"],
                description={
                    "P": "pressure", "rho": "fluid density", "v": "fluid velocity",
                    "A": "cross-sectional area", "h": "elevation / height",
                    "g": "gravitational acceleration", "Q": "volumetric flow rate",
                },
                subscript_convention=(
                    "Use P1/v1/A1/h1 vs P2/v2/A2/h2 when comparing two points."
                ),
            ),
            normalization_map={
                "pressure": "P", "density": "rho", "velocity": "v", "speed": "v",
                "area": "A", "height": "h", "elevation": "h", "gravity": "g",
                "flow rate": "Q",
            },
            parser_prompt_template="TEMPLATE",
            solver_hints=SolverHints(
                constants={"g": 9.81},
                augmented_givens={"g": 9.81},
                non_trivial_keywords=[
                    "pressure", "velocity", "density", "area", "height", "flow",
                    "fluid", "equation", "bernoulli", "continuity", "energy",
                    "incompressible", "horizontal", "pipe",
                ],
                plan_markers=[
                    "first", "then", "next", "step 1", "step 2", "solve for",
                    "substitute", "plug in",
                ],
            ),
            forbidden_named_laws=BERNOULLI_FORBIDDEN,
        ),
        source_document_id="seed",
        scope_embedding=[0.0] * 3072,
        policy_frozen=True,
    )
    return await load_concept("fluid_mechanics", "bernoulli_principle", neo)
