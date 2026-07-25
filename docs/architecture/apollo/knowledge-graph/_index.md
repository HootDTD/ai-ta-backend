---
doc: apollo/knowledge-graph/_index
description: Router for the Neo4j-backed KG persistence area, and home of the Neo4j-degraded behavior invariant.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Apollo knowledge-graph — Neo4j persistence

Persists per-attempt student subgraphs and the rebuildable reference projection
in Neo4j. All shapes are `apollo.ontology` types.

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [store](store.md) | `KGStore` per-attempt CRUD + Postgres-owned freeze; the handler-facing seam | `apollo/knowledge_graph/store.py`, `apollo/knowledge_graph/__init__.py` |
| [canon-projection](canon-projection.md) | Live `:Canon` MERGE seeder (WU-3C1) from Layer-1 entities | `apollo/knowledge_graph/canon_projection.py` |
| [resolution-store](resolution-store.md) | `RESOLVES_TO` + resolution-field persistence (WU-3C2, dormant) | `apollo/knowledge_graph/resolution_store.py` |

## Cross-cutting invariants

- **Neo4j-degraded behavior (domain-wide).** The Neo4j client is a process
  singleton built by `conversation/routing/router.md::get_neo4j_client`, which
  returns **`None`** when the driver is unreachable. `KGStore` never silently
  degrades: every Neo4j-backed method calls `_require_neo` at entry and raises
  `KGUnavailableError` (surfaced as **503**) when the client is `None`;
  Postgres-only methods (freeze/unfreeze, `get_node_trace`) keep working. The
  **decision to degrade** lives one layer up in the conversation handlers —
  `handle_chat` reads degrade to an empty graph and writes are skipped so a
  teaching turn still proceeds, while KG-native mutations (negotiate P3) surface
  the 503 because no safe degraded operation exists. Prod runs full Neo4j Aura;
  staging/dev run Neo4j-degraded against local Docker.
- **Scoping.** Subgraphs are scoped by an `attempt_id` property plus the
  secondary `:_KGNode` label; `:Canon` nodes are keyed on the BIGSERIAL
  surrogate `apollo_kg_entities.id`, never a synthesized string.
