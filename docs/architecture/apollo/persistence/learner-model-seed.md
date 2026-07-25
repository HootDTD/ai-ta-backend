---
doc: apollo/persistence/learner-model-seed
description: Pure, DB-free, LLM-free conversion core turning hand-authored Layer-1 source JSON into learner-model row specs, plus the executable reference-graph validation contract.
owns:
  - apollo/persistence/learner_model_seed.py
related:
  - apollo/persistence/_index
  - apollo/persistence/models
  - apollo/schemas/problem
  - platform/ops-seed-scripts
last_verified: 2026-07-25
stub: false
---

# apollo/persistence/learner-model-seed

The deterministic heart of the Apollo Layer-1 learner-model seed (spec §8/§5/§6.1).
It turns hand-authored source JSON (`concept_dag.json`, `canonical_symbols.json`,
`normalization_map.json`, `problem_*.json`, `misconceptions.json`) into Layer-1
row specs. NO SQLAlchemy import lives here — the functions take plain parsed dicts
and return frozen dataclasses / new dicts, so the fast unit suite needs no DB.

## Interface

Imported by 9 modules (provisioning, `resolution/candidates`,
`learner_model/personalization_select`, `schemas/problem`).

- **Value objects (frozen):** `EntitySpec` (one pre-DB `app.learner_entities` row:
  `canonical_key`/`kind`/`display_name`/`payload`/`aliases`),
  `ReferenceGraphValidation`, `NormalizedPath`; `SeedError` (no-fallback
  precondition guard).
- **`_ENTRY_TYPE_TO_KIND_PREFIX`** — the FROZEN `entry_type → (kind, key-prefix)`
  map (`equation→eq`, `condition→cond`, `simplification→(condition, simp)`,
  `procedure_step→(procedure, proc)`, `definition→def`,
  `variable_mapping→(variable, varmap)`); imported by
  `personalization_select.reference_entity_keys`.
- **Conversion fns:** `concept_dag_to_entities`, `concept_dag_to_prereqs`,
  `symbols_to_entities`, `derive_entity_key(entry_type, node_id) → key|None`
  (imported by `schemas/problem.to_kg_graph`), `reference_solution_to_entities`,
  `misconceptions_to_entities`, `authored_definitions` /
  `authored_definitions_from_spec`, `annotate_reference_solution` (returns a NEW
  dict adding per-step `entity_key` + `declared_paths` + `layer1_seeded`),
  `normalize_declared_paths`.
- **`validate_reference_graph(problem) → ReferenceGraphValidation`** — the
  executable §6.1 contract WU-4A grading consumes: every reference node must carry
  an `entity_key` and appear on a non-empty declared path covering every node;
  object-shaped paths additionally require a milestone that is a DEPENDS_ON sink.

## Data flow

Registry/problem JSON → conversion fns → `EntitySpec`/prereq specs → the DB write
layer `scripts/seed_apollo_learner_model.py` (`platform/ops-seed-scripts`, out of
scope here) upserts them into `app.learner_entities` / `internal.entity_prerequisites`.
`derive_entity_key` and `_ENTRY_TYPE_TO_KIND_PREFIX` are also called live (not
just at seed time) by the schema and personalization layers.

## Invariants & gotchas

- **Pure & immutable:** no SQLAlchemy / LLM / Neo4j; frozen dataclasses;
  `annotate_reference_solution` deep-copies and never mutates its input.
- **DB-13:** misconception `EntitySpec`s are **observability-only** — never
  persisted as `learner_entities` rows (the DDL's `kind` CHECK has no
  `misconception`); they surface as `MintPlan.misconception_keys`.
- `derive_entity_key` never raises (returns `None` for an unknown `entry_type`);
  the internal `_entity_key_for_step` variant DOES raise `KeyError` because its
  callers require a definite `str`.
- DRIFT: `EntitySpec`'s docstring says "migration-026 `apollo_kg_entities` row" —
  the live target table is `app.learner_entities` (use that name).

## Related

`apollo/persistence/models` (`LearnerEntity`/`EntityPrereq` targets),
`apollo/schemas/problem` (`derive_entity_key` consumer), `platform/ops-seed-scripts`
(the DB writer script).
