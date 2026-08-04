---
doc: apollo/overseer/coverage
description: DORMANT V3 KG-vs-KG coverage matcher — superseded as grader of record, no runtime caller.
owns:
  - apollo/overseer/coverage.py
related:
  - apollo/overseer/transcript-coverage
  - apollo/resolution/content-tiers
  - apollo/solver/sympy-exec
last_verified: 2026-08-04
stub: false
---

# Overseer coverage — dormant V3 KG-vs-KG matcher

> **DORMANCY BANNER.** `compute_coverage` has **no non-test runtime caller**.
> [transcript-coverage](transcript-coverage.md) superseded it as grader of
> record. It survives only as (a) an emitter of the frozen `coverage_contract`
> verdict shape and (b) the last importer of `resolution.tiers`. Do not treat it
> as part of the live grading path.

## Interface

- `compute_coverage(student_graph, reference_graph) -> dict` — concurrent
  KG-vs-KG matcher producing the same `per_step` / `procedure_scores` /
  `confidences` / `negotiation_counts` verdict shape as the live grader.

## Data flow

Compares a frozen student `KGGraph` against the reference `KGGraph`: one batched
binary LLM call per binary type (equation / condition / simplification) plus one
procedure-match call per procedure step, run concurrently via `asyncio.to_thread`
and `gather`. Both call sites build their client via `bounded_client()`
(`agent/llm-client`, 2026-08-04 — was a bare `OpenAI()`) even though this module
is dormant on the live grading path. Equation verdicts pass through a SymPy sign pre-gate reusing
`solver.sympy_exec.parse_zero_form` and `resolution.tiers` helpers
(`_extended_locals` / `_symbolic_equiv` / `_zero_form` / `student_surface_text`).
`negotiation_counts` are derived from student-node `DUAL`/`DISPUTED` status.

## Invariants & gotchas

- **DEPENDS_ON-direction-invariant:** compares by content and consults only
  outgoing `USES` neighbors for procedure evidence; ordering via `PRECEDES`.
- **No-fallback:** retry-with-backoff (3 attempts) then `CoverageGradingError`;
  `gather` never suppresses it.
- A `covered=true` verdict below `_BINARY_CONFIDENCE_FLOOR` (0.5) is downgraded to
  missing and logged `coverage_uncertain`.
- The sign pre-gate is downgrade-only (never upgrades a `covered=false`).

## Related

Grading-lane / dormancy context lives in [_index](_index.md).
