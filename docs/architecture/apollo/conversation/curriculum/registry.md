---
doc: apollo/conversation/curriculum/registry
description: Filesystem-backed subject/concept authoring registry and the single authority on the on-disk concept-pack schema.
owns:
  - apollo/subjects/__init__.py
  - apollo/subjects/calculus_2/__init__.py
  - apollo/subjects/fluid_mechanics/__init__.py
  - apollo/subjects/macroeconomics/__init__.py
related:
  - apollo/conversation/curriculum/db
  - apollo/conversation/parser/parser-llm
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

`apollo/subjects/__init__.py` is the filesystem-backed subject+concept registry —
the **authoring source of truth**. The three subject `__init__.py` files are
package-marker glue that ride here.

## Interface

- `load_concept(subject_id, concept_id) -> ConceptDefinition`.
- `list_subjects() -> list[str]`, `list_concepts(subject_id) -> list[str]`.
- Pydantic models `ConceptDefinition`, `CanonicalSymbols`, `SolverHints`,
  `ForbiddenNamedLaws` (+ `all_terms()`); `ConceptNotFoundError`.

## Data flow

`load_concept` reads a concept pack from
`apollo/subjects/<subject>/concepts/<concept>/` — `canonical_symbols.json`,
`normalization_map.json`, `parser_prompt_template.md`, `solver_hints.json`,
`forbidden_named_laws.json` (optional), and `problems/` — into a `ConceptDefinition`.
`_concept_dir` builds the path off `_SUBJECTS_ROOT` (the package dir).

## Invariants & gotchas

- **AUTHORING-ONLY at runtime.** The live selection/loader path is
  `curriculum/db` reading `app.concepts`; `scripts/seed_apollo_concept_registry.py`
  walks these on-disk packs and projects them into `app.concepts` (the FS→DB
  bridge — it does its own filesystem walk, not `load_concept`). The registry
  module is not on the live request path, but its `ConceptDefinition` + pydantic
  sub-models are the **shared shape** that `curriculum_db.load_concept_definition`
  reconstructs from DB rows and that `parser/parser-llm` consumes as a type.
- **Concept-pack JSON authority (R6):** the per-concept JSON packs +
  `parser_prompt_template.md` + `problems/problem_NN.json` + `AUTHORING.md` are
  authoring DATA (not `.py`), outside the ownership bijection but authored and
  described HERE. This doc is the single authority for the on-disk concept-pack
  schema; the database/domain docs and the seed script cross-link here rather than
  redefining it.
- `ForbiddenNamedLaws.all_terms()` unions four lowercased lists (named laws,
  concepts, domains, units) — consumed by the now-vestigial output-filter pre-filter.

## Related

Runtime loader `curriculum/db`; type consumer `parser/parser-llm`; DB projection
target `persistence/models` (the `Concept` model).
