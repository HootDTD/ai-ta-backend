---
doc: apollo/provisioning/path-enumeration
description: Fail-safe enumeration of alternative teach-back solution strategies over the minted graph.
owns:
  - apollo/provisioning/path_enumeration.py
related:
  - apollo/provisioning/promote
  - apollo/persistence/learner-model-seed
last_verified: 2026-07-25
stub: false
---

# provisioning/path-enumeration

Fail-safe, subject-agnostic enumeration of alternative teach-back solution strategies over
the minted reference graph. Default-OFF; consumed by `promote` (the `declared_paths`
replacement) and `authored_sets/api`.

## Interface

- `enumerate_strategy_paths(problem, *, chat_fn) -> list[dict]` — one structured call returning a valid replacement path set (or `[]`).
- `build_path_enumeration_schema() -> dict` — the strict closed schema for the enumeration call.
- `multi_path_enabled() -> bool` — the per-call `APOLLO_MULTI_PATH` reader.

## Data flow

`enumerate_strategy_paths` sends a step summary through one structured call, normalizes each
returned strategy through `learner_model_seed.normalize_declared_paths`, and returns the set
ONLY if it has ≥2 strategies AND the whole `{**problem, declared_paths: …}` passes
`validate_reference_graph`; otherwise it returns `[]`. `promote` gates the whole call on
`multi_path_enabled()` and keeps the legacy all-node path on any empty/invalid result.

## Invariants & gotchas

- **v1 can only route over steps ALREADY in the graph** — strategies needing new steps are
  deliberately deferred, and qualitative/prose solutions almost always return `[]`.
- The returned strategies are a complete REPLACEMENT for the legacy all-node path: they must
  jointly cover every step id, each must milestone ≥1 final-result sink, and no strategy's
  node set may equal or be a subset of another's (enforced by `validate_reference_graph`).
- Fail-safe: any degenerate/invalid enumeration returns `[]`, never a partial path set —
  enumeration can never block promotion.

## Env flags

- `APOLLO_MULTI_PATH` (default OFF) — gates the whole enumeration path.

## Related

- `provisioning/promote` — the `path_enumerator` consumer.
- `apollo/persistence/learner-model-seed` — `normalize_declared_paths` / `validate_reference_graph`.
