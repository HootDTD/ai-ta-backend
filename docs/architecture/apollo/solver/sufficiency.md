---
doc: apollo/solver/sufficiency
description: The dormant sufficiency check and its forward-chain planner; the SufficiencyVerdict type is still live in signatures.
owns:
  - apollo/solver/sufficiency.py
  - apollo/solver/forward_chain.py
related:
  - apollo/solver/sympy-exec
  - apollo/solver/narrator
last_verified: 2026-07-25
stub: false
---

# Solver — sufficiency (DORMANT)

> **DORMANCY BANNER.** `check_sufficiency` and `solve_kg_against_problem` have
> **no non-test caller**, and no runtime site constructs or passes a
> `SufficiencyVerdict`. The subsystem is designed-but-dormant. The one live
> thread is the **`SufficiencyVerdict` type**, imported by
> [agent/output-filter](../conversation/agent/output-filter.md) and threaded
> through its `_check_sufficiency_alignment` signature as an `Optional` default
> that is always `None`. Keep the type contract intact.

Answers "is the student's KG enough to solve the problem yet?" as a cheap,
deterministic per-turn signal — the designed per-turn analog of what coverage
does at Done time, with no new LLM call.

## Interface

- `check_sufficiency(*, kg, problem, reference_graph=None) → SufficiencyVerdict`
  — composes the forward-chainer with a reference-graph diff.
- `SufficiencyVerdict` — frozen dataclass: `state`
  (`sufficient`/`almost`/`insufficient`), `missing_variables`,
  `missing_kg_nodes`, `next_premise_hint`, `confidence`, `trace`.
- `solve_kg_against_problem(kg, problem)` (forward_chain.py) — parses each KG
  equation to zero-form and calls `solve_system` with the problem's
  givens/target; returns the solver status dict.

## Data flow

`check_sufficiency` runs `solve_kg_against_problem` (soft-failing to
`insufficient` on a parse error), then classifies: `sufficient` when SymPy
solves and the reference diff is empty; `almost` when solved-but-rubric-gap or
exactly one missing variable has an unmet defining equation in the reference KG;
else `insufficient`. `_diff_missing` matches student vs reference equations by a
whitespace/case-stripped signature and ranks unmet nodes by `PRECEDES` order to
pick `next_premise_hint`.

## Invariants & gotchas

- No DB, no I/O, no LLM — a pure verdict over dicts + an optional `KGGraph`.
- The `≤1 missing reference` gate stops an empty KG from flipping to `almost`
  just because a reference equation mentions the target symbol.
- Because the value is never produced at runtime, treat the module as a spec of
  the intended signal, not live behaviour — but do **not** delete the type.

## Related

- [solver/sympy-exec](sympy-exec.md) — `parse_zero_form` / `solve_system`
  underneath the forward-chainer.
- [solver/narrator](narrator.md) — renders the same solver trace.
