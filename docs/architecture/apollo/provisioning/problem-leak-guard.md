---
doc: apollo/provisioning/problem-leak-guard
description: GEN-1 two-layer answer-leak guard for generated problem statements.
owns:
  - apollo/provisioning/problem_leak_guard.py
related:
  - apollo/provisioning/problem-generation/_index
  - apollo/provisioning/problem-generation/generator
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

# provisioning/problem-leak-guard

GEN-1 two-layer answer-leak guard for generated problem STATEMENTS. It physically lives in the
provisioning root but is conceptually part of `problem_generation` — only `generator.py`
consumes it (cross-linked from `problem-generation/_index`).

## Interface

- `check_problem_leak(problem, *, chat_fn=None) -> ProblemLeakVerdict` — the guard entry.
- `ProblemLeakVerdict` — `leaked` / `confidence` / `reasons` / `method` (`deterministic`|`judge`).
- `CONFIDENCE_THRESHOLD` — the judge block threshold (0.6).

`ProblemLeakVerdict` and `check_problem_leak` are re-exported by the package facade
(`provisioning/_index`).

## Data flow

Layer 1 (deterministic) extracts solved target values + explicit final-step results, IGNORES
every `given_values` value, and looks for equivalent numbers or a target equation in
`problem_text` (0.5% numeric relative tolerance for display rounding). Layer 2 (the abstention
path) runs only when Layer 1 cannot extract an answer: an injected `MeteredChat.cheap`-shaped
`chat_fn` judges via strict structured output at temperature 0.

## Invariants & gotchas

- **Layer 1 is deliberately high precision** — a problem whose reference solution has no
  extractable answer ABSTAINS rather than guessing from prose. `target = given` is still a leak
  even when the scalar is also a given (the bare given stays legitimate).
- **Layer 2 passes OPEN on uncertainty** — parse errors and low-confidence positives are
  advisory and pass open, because generated problems are held for teacher review downstream; the
  judge blocks only at `CONFIDENCE_THRESHOLD`.
- Omitting `chat_fn` requests a deterministic-only check (an abstention returns a clean advisory
  verdict with reason `"no extractable answer"`).

## Related

- `provisioning/problem-generation/_index` — the GEN pipeline this guard belongs to.
- `provisioning/problem-generation/generator` — the sole consumer (`generate_problem_variants`).
- `apollo/schemas/problem` — `Problem` / `ReferenceStep`.
