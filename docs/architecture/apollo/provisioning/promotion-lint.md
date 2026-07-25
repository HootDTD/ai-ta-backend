---
doc: apollo/provisioning/promotion-lint
description: The pure nine-gate promotion lint — the auto-provisioning safety core (DB/LLM/ORM-free).
owns:
  - apollo/provisioning/promotion_lint.py
related:
  - apollo/provisioning/promote
  - apollo/provisioning/dedup
  - apollo/solver/sympy-exec
  - apollo/ontology/graph
  - apollo/persistence/learner-model-seed
last_verified: 2026-07-25
stub: false
---

# provisioning/promotion-lint

The §8B.4 PURE nine-gate promotion lint — the auto-provisioning SAFETY CORE. Before an
auto-scraped problem is promoted Tier-1 → Tier-2, it must pass nine gates run IN ORDER,
short-circuiting on the first failure. DB-free, LLM-free, ORM-free: the gate-4 symbols and
gate-8 hashes are PASSED IN by the caller (`promote`).

## Interface

- `run_promotion_lint(graph, *, canonical_symbols, normalization_map, existing_problem_hashes, active_gates=ALL_PROMOTION_GATES) -> PromotionResult`
- `content_active_gates(graph) -> frozenset[int]` — the content-derived applicable-gate subset.
- `ALL_PROMOTION_GATES` — the full 1..9 gate universe.
- `PromotionResult` (+ `PromotionVerified` / `PromotionRefuted` / `PromotionUnresolved`) — the frozen outcome.

`PromotionResult` and `run_promotion_lint` are re-exported by the package facade
(`provisioning/_index`); `PromotionUnresolved` + `content_active_gates` are imported by `promote`.

## Data flow

Gate 1 (schema) always runs — it `Problem.model_validate`s `graph` and builds the `Problem`
+ `KGGraph` the later gates reuse. Gates 2-8 run in a short-circuiting loop skipping any not
in `active_gates`; gate 9 runs last when a governing system + stated answer are present.
`content_active_gates` derives the applicable set: the structural core `{1,2,3,5,8}` always,
and the symbolic-rigor gates `{4,6,7}` self-activate ONLY when a parseable equation is present.

## Invariants & gotchas

- **The gates (in order):** 1 schema + mint-map membership; 2 closure
  (`validate_reference_graph` verbatim); 3 DAG acyclicity; 4 the SOLE foreign-symbol guard
  (reads the passed-in `canonical_symbols`/`normalization_map`); 5 procedure chain + terminal
  computes `target_unknown`; 6 SymPy `parse_zero_form` (malformed syntax only, auto-creates
  unknown symbols); 7 system closure (a PAPER free-symbol check, an honest v1 limit — not an
  end-to-end solve); 8 duplicate (`problem_dup_hash` membership in the caller's concept-scoped
  set); 9 solve/check (multiprocessing solve-with-timeout → verified/refuted/unresolved).
- **`unresolved` is a distinguished non-pass** the orchestrator holds for review — not a pass.
- Gate 1 ALWAYS runs regardless of `active_gates`; a rigor gate can only ever REJECT content
  it applies to, never block a subject it does not apply to (the subject-fluid design).
- Pure / DB-free / LLM-free: `canonical_symbols`/`normalization_map` (gate 4) and
  `existing_problem_hashes` (gate 8) are passed in — this unit never queries the DB. It owns
  the gate logic + diagnostic only; it does NOT promote, project `:Canon`, or persist rejections.

## Related

- `provisioning/promote` — the caller that supplies the active-gate set + concept symbols/hashes.
- `provisioning/dedup` — gate 8 uses `problem_dup_hash`.
- `apollo/solver/sympy-exec` — gate 6/9 use `parse_zero_form`.
- `apollo/ontology/graph` — gate 3 uses `KGGraph.topological_order`; gate 1 uses `EDGE_ALLOWED_PAIRS`.
- `apollo/persistence/learner-model-seed` — gate 2 uses `validate_reference_graph`.
