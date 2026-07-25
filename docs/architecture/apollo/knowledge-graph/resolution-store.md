---
doc: apollo/knowledge-graph/resolution-store
description: WU-3C2 persistence for the §5 resolver — RESOLVES_TO edges plus the four Layer-2 resolution node-fields (dormant).
owns:
  - apollo/knowledge_graph/resolution_store.py
related:
  - apollo/resolution/result
  - apollo/resolution/resolver
  - apollo/knowledge-graph/canon-projection
last_verified: 2026-07-25
stub: false
---

# Knowledge-graph — resolution store (DORMANT)

> **DORMANCY BANNER (verified in staging).** `write_resolution`,
> `write_resolves_to`, and `persist_resolution_fields` have **no non-test
> caller** — the resolver persistence leg is built but not wired into the live
> Done path; it is referenced only as an error-stage label in `apollo/errors.py`
> (`ResolutionUnavailableError`). The live grading path is
> `transcript_coverage` + `topic_score`, not the resolver.

Sibling of [store](store.md), split out purely to keep both files under the
~800-line ceiling. Persists the output of the §5 resolver.

## Interface

- `write_resolution(neo, attempt_id, result, *, resolved_at)
  → ResolutionWriteResult` — writes edges + fields in two idempotent passes.
- `write_resolves_to(neo, attempt_id, specs) → int` — MERGE the cross-label
  edges.
- `persist_resolution_fields(neo, attempt_id, specs) → int` — SET the four node
  fields.
- Pure DB-free mappers `resolved_node_to_edge_spec` / `resolved_node_to_field_spec`
  and the frozen `ResolvesToEdgeSpec` / `ResolutionFieldSpec` /
  `ResolutionWriteResult`. Consumes `resolution.result.{ResolutionResult,
  ResolvedNode}`.

## Data flow

Writes an idempotent
`(:_KGNode {attempt_id, node_id})-[:RESOLVES_TO {method, confidence,
resolved_at}]->(:Canon {key})` MERGE, plus `SET`s of `resolution` /
`resolution_method` / `resolution_confidence` on each `:_KGNode`, with
`resolved_key` set-or-removed per the None-omission convention. Edges are emitted
only for resolved nodes carrying a `:Canon` key; fields are written for **every**
node (unresolved included — the unresolved state is itself persisted data).

## Invariants & gotchas

- `RESOLVES_TO` is a `:_KGNode → :Canon` cross-label edge with a fixed shape,
  written by its own Cypher — deliberately **not** a member of `EdgeType` /
  `EDGE_ALLOWED_PAIRS` (those constrain within-subgraph edges via
  `store.write_edges`).
- MERGE (not CREATE) makes re-resolution idempotent: same endpoints → same edge,
  props overwritten.
- NO-FALLBACK: any Neo4j failure raises `ResolutionUnavailableError` with the
  offending `stage`; because resolution runs after the grade is committed, the
  failure is loud but never voids it — a later Done/janitor retry re-runs the
  idempotent writes.

## Related

- [resolution/result](../resolution/result.md) — the `ResolutionResult` this
  maps to specs.
- [resolution/resolver](../resolution/resolver.md) — produces that result.
- [knowledge-graph/canon-projection](canon-projection.md) — seeds the `:Canon`
  targets.
