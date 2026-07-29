---
doc: apollo/provisioning/provisioning-schema
description: The prompt-to-parser json_schema contract plus the single shared generation ontology block.
owns:
  - apollo/provisioning/provisioning_schema.py
  - apollo/provisioning/generation_contract.py
related:
  - apollo/ontology/nodes
  - apollo/schemas/problem
  - apollo/provisioning/solution
last_verified: 2026-07-25
stub: false
---

# provisioning/provisioning-schema

The prompt↔parser contract shared by every provisioning LLM call-site — pure data
builders, no I/O. `provisioning_schema` derives strict/declared `json_schema` envelopes
FROM the Pydantic models so a schema can never drift from the code; `generation_contract`
owns the single per-step ontology block every reference-solution prompt appends.

## Interface

`provisioning_schema.py`
- `build_solution_schema(*, augmentation=False)` — Stage-2 envelope, `strict=False` (see below).
- `build_tag_schema()` — Stage-4 concept tag (`strict=True`).
- `build_pairing_phase_a_schema()` / `build_pairing_phase_b_schema()` — Stage-3 judge schemas (`strict=True`).
- `solution_content_field_hints()` — per-`entry_type` content-field prose from `NODE_CONTENT_TYPES`.
- `REFERENCE_STEP_FIELDS`, `ENTRY_TYPES` — field/enum sets derived from the models.

`generation_contract.py`
- `ontology_block()` — the shared, framing-free per-step output contract prompts append verbatim.
- `SHARED_CONSTANT_SYMBOLS` — the `{pi, e, g, c, R, k_B, N_A}` ambient-constant whitelist.
- `GENERIC_ID_TOKENS` — the opaque-id vocabulary (`step`, `eq`, `node`, …) the id-quality defect uses.

## Data flow

`REFERENCE_STEP_FIELDS = tuple(ReferenceStep.model_fields)` and
`ENTRY_TYPES = tuple(NODE_CONTENT_TYPES)` — the schemas are BUILT from the models, and the
contract tests pin that the `items.required` key set equals `REFERENCE_STEP_FIELDS`.
`ontology_block()` sources `solution_content_field_hints()` (hence `NODE_CONTENT_TYPES`),
so a rule added here propagates to `solution.py`, `authored_sets/graph_derivation.py`, and
authored construction at once (the DAG-3 single-home fix).

## Invariants & gotchas

- **Stage-2 cannot be strict-closed.** `ReferenceStep.content` is an OPEN per-`entry_type`
  dict (and `given_values` keys are arbitrary symbols), so `build_solution_schema` declares
  `content` a permissive object and runs `strict=False`; `Problem.model_validate` is the
  HARD post-parse enforcer. The Stage-4 tag schema has no open dicts → `strict=True`.
- **Never hand-type the field lists.** Adding/removing a model field red-flags the contract
  tests through the derived sets.
- `ontology_block()` renders byte-stably from the ontology, so prompt-hash tests are possible
  and the prose can never drift from `NODE_CONTENT_TYPES`.
- `SHARED_CONSTANT_SYMBOLS` exists so an ambient constant in an equation does not fabricate a
  false dependency edge (the dependency-completeness defect whitelist).

## Related

- `apollo/ontology/nodes` — `NODE_CONTENT_TYPES`, the source of the entry-type enum + hints.
- `apollo/schemas/problem` — `ReferenceStep` / `Problem`, the schema of record.
- `provisioning/solution` — the primary consumer of `build_solution_schema` + `ontology_block`.
