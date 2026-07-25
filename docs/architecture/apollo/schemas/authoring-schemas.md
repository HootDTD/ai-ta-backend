---
doc: apollo/schemas/authoring-schemas
description: Three small standalone Pydantic schemas for hand-authored Apollo curriculum JSON — the concept DAG, a procedure step, and a variable map — currently test-only.
owns:
  - apollo/schemas/dag.py
  - apollo/schemas/procedure.py
  - apollo/schemas/variable_map.py
related:
  - apollo/schemas/_index
  - apollo/schemas/problem
  - apollo/persistence/learner-model-seed
last_verified: 2026-07-25
stub: false
---

# apollo/schemas/authoring-schemas

Three small standalone Pydantic schemas for hand-authored Apollo curriculum JSON,
grouped because each is tiny and none has a live runtime importer (R3
small-dormant-cohesive group).

## Interface

- **`dag.py`** — the concept-hierarchy DAG file schema: `DagNode`
  (`id`/`label`/`prerequisites`/`scope_boundary`/`topic_cluster`), `DagEdge`
  (`type` `requires`|`extends`|`excludes`, `from`/`to` with a `from` alias), `Dag`
  (a `_unique_node_ids` validator + `validate_edge_targets` referential check),
  and `load_dag(path)`.
- **`procedure.py`** — `ProcedureStep` (`order` ≥ 1, `action`, `uses_equations`,
  `purpose`): a single ordered plan step. NOTE this is **not** the type
  `ontology/nodes.py` uses (that module defines its own
  `ProcedureStepContent`/`ProcedureStepNode`), and `problem.py` handles procedure
  steps via inline dict content — so this class is imported only by its own test.
- **`variable_map.py`** — `VariableMap` (`topic_cluster` + a `mappings` dict of
  natural-language term → canonical SymPy symbol name) and `load_variable_map(path)`.

## Invariants & gotchas

- Immutable pydantic models; the `load_*` helpers validate on read.
- **DORMANT / candidate-dead:** all three are imported ONLY by their own tests —
  no apollo runtime importer. Document them, but deprioritize; a deletion is an
  owner decision, not assumed here.

## Related

`apollo/schemas/problem` (the live central schema),
`apollo/persistence/learner-model-seed` (consumes `concept_dag`/`normalization`
JSON as plain dicts, NOT via these classes).
