---
doc: ai-ta-backend/apollo/provisioning/authored-sets/graph-derivation
description: Derives a gold-format reference graph FROM a paired worked solution (reversed provisioning), guarded by a pure defect validator
owns:
  - apollo/provisioning/authored_sets/graph_derivation.py
related:
  - ai-ta-backend/apollo/provisioning/authored-sets/_index
  - ai-ta-backend/apollo/provisioning/authored-sets/orchestrator
  - ai-ta-backend/apollo/provisioning/solution
  - ai-ta-backend/apollo/provisioning/provisioning-schema
  - ai-ta-backend/apollo/schemas/problem
  - ai-ta-backend/apollo/solver/sympy-exec
last_verified: 2026-07-25
stub: false
---

## Interface

- `derive_reference_graph(candidate, spans, *, concept_slug, concept_display_name,
  canonical_symbols, normalization_map, chat_fn)` → `DerivedGraph` — one derivation
  call plus one defect-feedback retry (called by the orchestrator's reversed path).
- `find_derivation_defects(graph, *, canonical_symbols, normalization_map,
  classes=None)` → list of `"category: detail"` — the pure validator. Also imported
  by `solution.find_or_generate`, which runs the `GENERATION_DEFECT_CLASSES` subset.
- `DerivedGraph`, `DerivationError`, `ALL_DEFECT_CLASSES`,
  `GENERATION_DEFECT_CLASSES`, `kc_granularity_enabled`.

## Data flow

Replaces the ungrounded `tag_and_mint` graph generation. `chat_fn` (main-tier,
inject `metered_chat.main`) sees only problem text + paired-solution span text +
concept vocabulary — one derivation at `reasoning_effort='medium'`, and if the
validator finds defects, one retry at `'high'` with the defect list fed back. Still
defective → `DerivationError` (fail-closed; the orchestrator rejects the candidate).

## Invariants & gotchas

- **Leak guard:** the prompt never sees other course material or learner state;
  every node must trace to the solution text. `validate_pair` independently judges
  faithfulness against the same spans.
- The validator enforces the calc-2 gold format: `Problem.model_validate`, node-count
  bounds (5–9 legacy / 3–15 KC-grained), unique MEANINGFUL snake_case ids (opaque-id
  + semantic-key echo checks), concrete equations parse under BOTH `sympy.sympify`
  and `parse_zero_form` with a concept `local_dict`, display/operator-identity
  content exempt from the parse rule, no variable fragmentation, transitive
  dependency completeness, an optional symbol table, and a Kahn-DAG `depends_on`.
- `GENERATION_DEFECT_CLASSES` drops `node_count` + `foreign_symbol` (a generation
  caller has no concept vocabulary, so foreign-symbol would flag every legit symbol).
- Every symbolic category self-deactivates on prose content (no parseable equations
  → no symbolic defects).

## Env flags

`APOLLO_KC_GRANULARITY` switches the node-count bounds + granularity prompt (per-call
read, no restart).

## Related

`generation_contract.ontology_block`/`SHARED_CONSTANT_SYMBOLS`/`GENERIC_ID_TOKENS`
(provisioning-schema), `solution.GroundingSpan`, `schemas.problem.Problem`,
`solver.sympy_exec.parse_zero_form`/`MalformedEquationError`.
