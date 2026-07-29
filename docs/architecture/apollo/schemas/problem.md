---
doc: apollo/schemas/problem
description: The central Apollo problem schema — a validated problem file with a structured reference solution, and its derivation into a typed reference KG.
owns:
  - apollo/schemas/problem.py
  - apollo/schemas/__init__.py
related:
  - apollo/schemas/_index
  - apollo/persistence/learner-model-seed
  - apollo/persistence/models
  - apollo/ontology/_index
  - apollo/ontology/graph
last_verified: 2026-07-25
stub: false
---

# apollo/schemas/problem

The central Apollo problem schema: a validated problem file with a structured
reference solution, plus its derivation into a typed reference KG. Imported by
20+ modules across handlers, provisioning, overseer, questioning,
`learner_model/personalization_select`, `hoot_bridge`, and
`persistence/models.py`.

## Interface

- **`ReferenceStep`** — `step`/`entry_type`/`id`/`content`/`depends_on` + an
  optional per-step `entity_key` (F-struct: flows onto the reference Node when
  present).
- **`Problem`** — `id`, `database_id` (the target-schema surrogate, **excluded**
  from API serialization so the public problem-code contract is unchanged),
  `concept_id`, `difficulty` (`intro`|`standard`|`hard`), `problem_text`,
  OPTIONAL `given_values`/`target_unknown` (subject-fluid Apollo),
  `reference_solution` (min-length 1).
- The **`EntryType`** literal (`equation`|`definition`|`condition`|
  `simplification`|`variable_mapping`|`procedure_step`) and `Difficulty` literal.
- **`load_problem(path) → Problem`**.
- **`_resolve_references`** (model validator) — every `depends_on` resolves to a
  real step id, every `procedure_step.uses_equations` resolves to a real equation
  id, and `procedure_step` `order` forms a 1..N contiguous sequence.
- **`to_kg_graph(attempt_id) → KGGraph`** — derives a typed reference subgraph:
  one `Node` per step (each carrying `entity_key`, authored or derived via
  `learner_model_seed.derive_entity_key`), `DEPENDS_ON` edges, `USES` edges
  (procedure_step → equation), and a `PRECEDES` chain across procedure steps in
  order. `_strip_legacy_proc_fields` drops `order`/`uses_equations` from procedure
  content (now edge-encoded).

## Data flow

Authored/provisioned problem JSON → `Problem.model_validate` → (at attempt time)
`to_kg_graph` builds the reference KG the grader diffs against. The
`persistence/models.Problem` ORM promotes these fields to typed columns via
`to_pydantic_payload` / `from_pydantic_payload`.

## Invariants & gotchas

- Immutable pydantic v2 models.
- **`DEPENDS_ON` uses the canonical prerequisite → dependent direction.** The
  parser layer intentionally emits the inverse (dependent → prerequisite);
  `build_student_canonical` is the boundary that flips student edges into this
  convention.
- `database_id` is excluded from serialization so the public problem-code
  contract stays unchanged.

## Related

`apollo/persistence/learner-model-seed` (`derive_entity_key`),
`apollo/ontology/_index` + `apollo/ontology/graph` (`Edge`/`EdgeType`/`KGGraph`/
`Node`/`NodeType`/`build_node`), `apollo/persistence/models` (`Problem` ORM
promotes these fields).
