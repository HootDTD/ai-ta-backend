---
doc: ai-ta-backend/rag-pipeline/solver
description: Sandboxed run_python utility — DORMANT in production (conceptual-only mode disables it).
owns:
  - ai/solver.py
related:
  - ai-ta-backend/rag-pipeline/main-ai
last_verified: 2026-07-25
stub: false
---

# solver — sandboxed Python execution (dormant)

## Interface

- `run_python(code, env=None) -> CodeResult` (dataclass: `stdout`, `globals`,
  `code_hash`, `vars_created`). Exposes only `np`, optional `sp` (sympy),
  `ureg` (pint) + a few default units. Imported only by `ai/main_ai.py`.

## Invariants & gotchas

- **DORMANT on the live path**: `solve_with_bundle` enforces conceptual-only mode
  (`final_answers={}`, no code), so numeric computation is deliberately disabled —
  the assistant teaches concepts, not homework arithmetic.
- Forbidden builtins (`open`, `__import__`, `eval`, `exec`, `input`) raise
  `RuntimeError("forbidden builtin")`. It is nonetheless an `exec` surface kept
  dormant. (The historical sympy `parse_expr` RCE concern was in the Apollo
  grader, not here.)

## Related

`main-ai` (sole importer).
