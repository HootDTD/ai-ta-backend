---
doc: apollo/solver/narrator
description: Template rendering of a solver trace into student-readable lines (dormant, part of the planner chain).
owns:
  - apollo/solver/narrator.py
related:
  - apollo/solver/sufficiency
  - apollo/solver/sympy-exec
last_verified: 2026-07-25
stub: false
---

# Solver — narrator (DORMANT)

> **DORMANCY BANNER.** `narrate_trace` has only test callers — it is part of the
> dormant forward-chain → sufficiency → narrator planner chain (see
> [solver/_index](_index.md)). Documented for the retained contract, not live
> behaviour.

Template-based natural-language rendering of a `solve_system` trace. **No LLM.**

## Interface

- `narrate_trace(trace, *, status, target, missing_variables=None) → str` —
  render the step trace produced by `solver.sympy_exec.solve_system` into
  student-readable lines.

## Data flow

Walks each trace op (`substitute_givens`, `solve_system`, `pick_real_solution`,
`parameterized_solution`, `target_absent`, `empty_kg`, `no_real_solution`) to a
line, wrapping math in `$…$` when a `*_latex` form is present and falling back to
plain text otherwise, then appends a solved/stuck closing line.

## Related

- [solver/sufficiency](sufficiency.md) — produces the verdict this would
  narrate.
- [solver/sympy-exec](sympy-exec.md) — emits the trace shape consumed here.
