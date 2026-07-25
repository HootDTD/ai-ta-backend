---
doc: apollo/provisioning/promote
description: Stage-6 Tier-1 to Tier-2 promotion — annotate, lint, flip tier, project Canon.
owns:
  - apollo/provisioning/promote.py
related:
  - apollo/provisioning/promotion-lint
  - apollo/provisioning/tag-mint
  - apollo/provisioning/path-enumeration
  - apollo/knowledge-graph/canon-projection
  - apollo/persistence/learner-model-seed
last_verified: 2026-07-25
stub: false
---

# provisioning/promote

Stage-6, the last of the per-document stages: `promote` annotates the minted reference
graph, runs the nine-gate lint over it, and on a PASS flips the Tier-1 row to Tier-2 and
projects `:Canon`. It reuses three frozen primitives and re-implements none.

## Interface

- `promote(db, neo, *, problem, mint_plan, search_space_id, concept_problem_id, existing_problem_hashes, solution_source=None, path_enumerator=None) -> PromoteResult`
- `PromoteResult` — `promoted` / `failed_gate` / `diagnostic`.
- `PromoteHeldForReview(PromoteResult)` — the distinguished gate-9 `unresolved` non-pass.

`promote` and `PromoteResult` are re-exported by the package facade (`provisioning/_index`).

## Data flow

`annotate_reference_solution` stamps each step's `entity_key` + a top-level `declared_paths`
(the gate-2 annotated-graph contract); `run_promotion_lint` runs the gates reading the
concept's authored `canonical_symbols`/`normalization_map` (gate 4) and the caller's
`existing_problem_hashes` (gate 8), with the applicable subset from `content_active_gates`;
`project_canon` does the idempotent `:Canon` MERGE. On PASS the annotated payload +
`solution_source` are stored, the row is re-homed onto the tagged concept, `tier` flips 1→2
keyed on the existing row id, the session flushes, then `:Canon` projects.

## Invariants & gotchas

- **Never inserts** — the tier flip is keyed on the existing row id (a stray insert would
  duplicate inventory). Both the flip and the `:Canon` MERGE are idempotent/replay-safe.
- **Never commits or rolls back** — the orchestrator owns the transaction. A
  `CanonProjectionError` is re-raised with the flushed tier flip left in place for idempotent
  re-projection.
- A malformed problem that would `KeyError` during pre-lint annotation is converted to the
  clean gate-1 rejection (one bad candidate must not sink the whole document).
- `solution_source` is written ONLY when the row has none (a re-promote never downgrades a
  pre-stamped `authored` row); a `verification` stamp records mechanically-verified vs
  faithfulness-only (whether the symbolic rigor gates were in the active set).

## Env flags

- `APOLLO_MULTI_PATH` — when on, `_with_enumerated_paths` replaces the legacy all-node
  `declared_paths` with enumerated object-paths, but ONLY if the whole replacement set passes
  `validate_reference_graph` (else the legacy path stays intact).

## Related

- `provisioning/promotion-lint` — the nine gates + `content_active_gates`.
- `provisioning/tag-mint` — supplies the `MintPlan` + minted entities promote annotates against.
- `provisioning/path-enumeration` — the optional `path_enumerator`.
- `apollo/knowledge-graph/canon-projection` — `project_canon`.
- `apollo/persistence/learner-model-seed` — `annotate_reference_solution` / `validate_reference_graph`.
