---
doc: apollo/resolution/result
description: The resolver's structured result models — ResolvedNode per student node and the aggregate ResolutionResult.
owns:
  - apollo/resolution/result.py
related:
  - apollo/resolution/resolver
  - apollo/knowledge-graph/resolution-store
last_verified: 2026-07-25
stub: false
---

# Resolution — result

> Part of the §5 resolver, currently **unwired** (see [_index](_index.md)).

The resolver's return value. Kept as its own low-level seam so
`knowledge_graph.resolution_store` can import it without a circular dependency.

## Interface

- `ResolvedNode` (frozen) — the outcome for one student evidence node:
  `node_id`, `resolution` (`resolved`/`unresolved`/`ambiguous`), `resolved_key`
  (matched candidate's `canonical_key`, `None` when unresolved),
  `resolved_canon_key` (the `:Canon` surrogate for the edge, `None` when
  unresolved or unprojected), `method`, `confidence` (already method-capped).
- `ResolutionResult` (frozen) — `resolved` (one entry per student node,
  unresolved included), `tier_counts` (per-method histogram), `llm_calls`, and
  `resolved_edges()` (the subset eligible for a `RESOLVES_TO` edge:
  `resolution == "resolved"` AND a non-null `resolved_canon_key`).

## Invariants & gotchas

- **A non-match is DATA**, not an error — unresolved nodes are carried in
  `resolved`, they simply produce no edge.
- **`llm_calls` MUST be ≤ 1** by contract (currently always 0 — the LLM
  adjudication path is retired).
- Immutable; kept separate from `resolution_store`'s Neo4j spec types to avoid a
  circular import (WU-4A also imports `result` directly).

## Related

- [resolution/resolver](resolver.md) — constructs these.
- [knowledge-graph/resolution-store](../knowledge-graph/resolution-store.md) —
  maps them to Neo4j specs.
