---
doc: apollo/solver/_index
description: Router for the SymPy solver area — one live symbolic-math seam plus a dormant planner chain.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Apollo solver — symbolic math

Deterministic SymPy math for the grading + provisioning stacks. **No LLM in this
area.**

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [sympy-exec](sympy-exec.md) | `parse_zero_form` / `solve_system` — the reused symbolic-math seam | `apollo/solver/sympy_exec.py`, `apollo/solver/__init__.py` |
| [sufficiency](sufficiency.md) | Sufficiency verdict + forward-chain planner (dormant value; live type) | `apollo/solver/sufficiency.py`, `apollo/solver/forward_chain.py` |
| [narrator](narrator.md) | `narrate_trace` template rendering of a solver trace (dormant) | `apollo/solver/narrator.py` |

## Cross-cutting invariants

- **Only `sympy_exec.py` is live.** `parse_zero_form` (+ `_local_dict`,
  `_tidy_floats`, `MalformedEquationError`) is a high-fan-in seam imported by
  `knowledge_graph/store`, `overseer/coverage`, `resolution/tiers`, and
  provisioning (`authored_sets/graph_derivation`, `promotion_lint`).
- **The planner chain is DORMANT.** `forward_chain.solve_kg_against_problem →
  sufficiency.check_sufficiency → narrator.narrate_trace` has only test callers.
  No runtime site constructs a `SufficiencyVerdict` — the **type** is imported by
  `agent/output_filter` and threaded through signatures as an unused `Optional`,
  so the type contract is load-bearing even though the value is never produced.
