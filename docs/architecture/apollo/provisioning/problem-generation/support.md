---
doc: apollo/provisioning/problem-generation/support
description: The two generator support modules — the variation-operator catalog and the post-generation round-trip/qualitative verifiers
owns:
  - apollo/provisioning/problem_generation/operators.py
  - apollo/provisioning/problem_generation/verifiers.py
related:
  - apollo/provisioning/problem-generation/_index
  - apollo/provisioning/problem-generation/generator
  - apollo/provisioning/promotion-lint
  - apollo/provisioning/metered-chat
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

## Interface

- **operators:** `VARIATION_OPERATORS` (the catalog), `VariationOperator`
  (dataclass with `name`/`prompt`/`applicable`/`include_dag_skeleton` +
  `build_messages`), plus the module-level prompt strings and
  `SHARED_OUTPUT_CONTRACT`.
- **verifiers:** `round_trip_check` → `RoundTripVerdict`, `qualitative_rubric` →
  `RubricReport` (`RubricClaim`). Both consumed only by `generator.py`.

## Data flow

Each operator pairs a stable prompt with a content-derived applicability predicate:
`parameter_perturbation` activates only for seeds with `given_values`;
`context_reskin` and `isomorphic_dag_shape` are always applicable and preserve
qualitative targets without inventing numbers (the DAG-shape operator passes only a
structure-only skeleton, never seed content). `round_trip_check` delegates to
`promotion_lint._gate_9` for a deterministic solve-back of the generated reference;
`qualitative_rubric` runs a strict structured LLM judge (temperature 0) that
decomposes the reference solution into atomic claims checked against the problem
statement alone.

## Invariants & gotchas

- `SHARED_OUTPUT_CONTRACT` forbids including the solution/answer in `problem_text` and
  forbids forcing equations/numbers onto prose problems.
- `round_trip_check` returns `inapplicable` when there is no distinct governing system
  + stated target answer (the generator then routes to the qualitative rubric).
- `qualitative_rubric` is advisory + fail-open (returns `None` on any error) but
  re-raises `CostBudgetExceeded` so the metering circuit-break stays visible; its
  ceiling is `faithfulness_only` (it never claims correctness).

## Related

`promotion_lint._answer_equation_step`/`_gate_9` (round-trip solve),
`schemas.problem.Problem`, `metered_chat.CostBudgetExceeded`.
