---
doc: apollo/ontology/graph
description: The KGGraph aggregate — the single source-of-truth graph shape — plus the ontology package facade.
owns:
  - apollo/ontology/graph.py
  - apollo/ontology/__init__.py
related:
  - apollo/ontology/nodes
  - apollo/ontology/edges
  - apollo/knowledge-graph/store
last_verified: 2026-07-25
stub: false
---

# Ontology — graph aggregate + package facade

`KGGraph` bundles `nodes` + `edges` into the single shape every backend module
consumes instead of the legacy bag-shaped dict-of-lists; the **frontend
`ApolloKG` mirrors it verbatim**. This leaf also owns the ontology package
facade (`__init__.py`), whose `__all__` is the stable public API of the area.

## Interface

- `KGGraph(BaseModel)` with `nodes: list[Node]`, `edges: list[Edge]` and helper
  methods:
  - lookups — `node_index()`, `by_type(node_type)`, `has_node(id)`;
  - traversal — `outgoing(id, edge_type?)`, `incoming(id, edge_type?)`,
    `neighbors(id, edge_type)`, `precedes_chain(start?)`,
    `topological_order(edge_type, node_type?)` (Kahn's algorithm; raises on a
    cycle);
  - subgraph ops — `merge(other)` (later nodes win by `node_id`),
    `filter_attempt(attempt_id)`.
- Package facade `apollo/ontology/__init__.py` re-exports the whole surface —
  `Node`, `NodeType`, `NodeSource`, the concrete `*Node`/`*Content` classes,
  `NODE_LABELS`/`NODE_LABEL_TO_TYPE`/`NODE_CONTENT_TYPES`, `build_node`, `Edge`,
  `EdgeType`, `EdgeProvenance`, `EDGE_ALLOWED_PAIRS`, and `KGGraph` — so callers
  write `from apollo.ontology import KGGraph, Node, build_node, …`.

## Data flow

Every `KGStore` read returns a `KGGraph` and every write takes typed `Node`/
`Edge` lists; the overseer graders, resolution, `topic_score`, the solver, and
`handlers/{chat,done}` all pass `KGGraph`. `precedes_chain` / `topological_order`
are how procedure ordering is recovered now that step `order` is no longer a
node field.

## Invariants & gotchas

- `topological_order` raises `ValueError` on a cycle in the induced subgraph —
  callers that walk `PRECEDES` must tolerate this (the store's summary uses the
  more forgiving `precedes_chain`, which stops at the first branch/cycle).
- `merge` is last-writer-wins by `node_id`; edges are concatenated, not deduped.

## Related

- [ontology/nodes](nodes.md), [ontology/edges](edges.md) — the members.
- [knowledge-graph/store](../knowledge-graph/store.md) — serialises `KGGraph`
  to/from Neo4j.
