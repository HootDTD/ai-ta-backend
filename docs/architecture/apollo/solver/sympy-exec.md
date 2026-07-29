---
doc: apollo/solver/sympy-exec
description: The shared SymPy wrapper — parse a zero-form equation and solve a system; the high-fan-in symbolic-math seam.
owns:
  - apollo/solver/sympy_exec.py
  - apollo/solver/__init__.py
related:
  - apollo/resolution/content-tiers
  - apollo/knowledge-graph/store
  - apollo/solver/sufficiency
last_verified: 2026-07-25
stub: false
---

# Solver — sympy-exec

The most-reused solver primitive: parse a student- or reference-taught equation
to a zero-form SymPy expression, and solve a simultaneous system. `__init__.py`
is empty namespace glue.

## Interface

- `parse_zero_form(symbolic, *, entry_id, local_dict=None)` — parse `LHS = RHS`,
  a chained equality (`A = B = C = …`), or a bare `LHS - (RHS)` form to a SymPy
  expression for `LHS - RHS`. Raises `MalformedEquationError` (from
  `apollo.errors`) attributed to `entry_id` on any parse failure.
- `solve_system(equations, givens, target)` — substitute givens, solve for the
  unknowns, and return a status dict (`solved` with a value, or `stuck` with
  `missing_variables`) plus a step `trace`.
- `_local_dict()` — the canonical fluid-mechanics symbol table (imported by
  `resolution/tiers._extended_locals` to extend without editing the solver).
- `_tidy_floats(expr)` — collapse whole-number Floats to Integers for display
  (imported by `knowledge_graph/store` for equation LaTeX rendering).
- `_format_value_text(val)` — human-friendly value rendering.

## Data flow

`parse_zero_form` normalises `^`→`Pow` via `convert_xor` and reduces a chained
equality to its **first** equality (the symbolic statement); the trailing
numeric/unit tail is discarded (debug-logged, never raised) — dropping it can
only under-constrain, never admit a wrong equation. `solve_system` builds a
substitution trace with LaTeX for each step and picks the first real solution
for `target`.

## Invariants & gotchas

- **All-or-nothing parse:** a malformed entry raises rather than being silently
  skipped, so a bad equation is attributed to a specific KG entry, never dropped.
- **Single mint/runtime parser (WU-AAS B2.2):** this is the *only* equation
  parser — the resolution tier delegates here with extended locals so a minted
  subject and the runtime grader parse identical notations. Do not fork a second
  parser.
- **HIGH FAN-IN:** `parse_zero_form` (+ `MalformedEquationError`, `_local_dict`,
  `_tidy_floats`) is imported by `knowledge_graph/store`, `overseer/coverage`,
  `resolution/tiers`, and provisioning `authored_sets/graph_derivation` +
  `promotion_lint`. A signature change here ripples across the grading and
  provisioning stacks.

## Related

- [resolution/content-tiers](../resolution/content-tiers.md) — reuses
  `parse_zero_form` / `_local_dict` for the symbolic tier.
- [knowledge-graph/store](../knowledge-graph/store.md) — uses `parse_zero_form`
  + `_tidy_floats` for equation LaTeX display.
- [solver/sufficiency](sufficiency.md) — the dormant forward-chain planner over
  `solve_system`.
