---
doc: apollo/schemas/_index
description: Router for the Apollo Pydantic content schemas — the central Problem schema plus three small hand-authored-JSON file schemas.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# apollo / schemas

The Apollo Pydantic content schemas — the structured shapes for hand-authored
curriculum/problem JSON.

## Leaf docs

| Doc | One-liner | Owns |
|---|---|---|
| [problem](problem.md) | The central `Problem`/`ReferenceStep` schema + reference-KG derivation (imported by 20+ modules) | `apollo/schemas/problem.py` (+`__init__.py` glue) |
| [authoring-schemas](authoring-schemas.md) | Three small file schemas (`Dag`/`ProcedureStep`/`VariableMap`), currently test-only | `apollo/schemas/dag.py`, `procedure.py`, `variable_map.py` |

## Cross-cutting invariants

- **`problem.py` is the live central schema**; the other three
  (`dag`/`procedure`/`variable_map`) are DORMANT — each is imported only by its
  own test, with no apollo runtime importer.
- `problem.to_kg_graph` emits `apollo.ontology` node/edge types and derives
  missing per-step keys via `learner_model_seed.derive_entity_key` — the two
  cross-sub-area dependencies these schemas rely on but do not own.

## Related

`apollo/persistence/learner-model-seed` (`derive_entity_key`, consumed by
`problem.py`), `apollo/ontology/_index` (`to_kg_graph` emits its Nodes/Edges),
`apollo/persistence/models` (the `Problem` ORM promotes this shape to columns).
