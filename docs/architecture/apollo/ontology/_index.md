---
doc: apollo/ontology/_index
description: Router for the Apollo V3 KG ontology — the typed shape layer every apollo module mirrors.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Apollo ontology — typed shape layer

`apollo.ontology` is the **single source-of-truth data contract** for the whole
Apollo KG pipeline. The parser, `KGStore`, the overseer graders
(`coverage`, `topic_score`, `transcript_coverage`), the `resolution` tiers, the
`solver`, and the provisioning schema all import these shapes rather than a
bag-shaped `dict`; the **frontend `ApolloKG` mirrors `KGGraph` verbatim**. This
makes it the most-linked area index in the tree — treat any change here as a
cross-cutting change.

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [nodes](nodes.md) | 6-node discriminated union + `build_node` + `NODE_LABELS` maps | `apollo/ontology/nodes.py` |
| [edges](edges.md) | 4-type `EdgeType` + `EDGE_ALLOWED_PAIRS` enforced at construction | `apollo/ontology/edges.py` |
| [graph](graph.md) | `KGGraph` aggregate + ontology package facade (`__all__`) | `apollo/ontology/graph.py`, `apollo/ontology/__init__.py` |

## Cross-cutting invariants

- **Node/edge shapes are the contract.** Every read/write across the pipeline
  passes a `KGGraph`; downstream modules never reconstruct node/edge dicts by
  hand. A field added to a node/edge type ripples to the store's Neo4j
  property mapping, the parser extraction schema, and the frontend mirror.
- **Ordering is edge-derived, not a field.** Procedure-step `order` and
  `uses_equations` were removed from nodes: sequence comes from `PRECEDES`
  edges and procedure→equation links are real `USES` edges (see
  [nodes](nodes.md) / [edges](edges.md)).
- **Edge legality is enforced at construction.** `EDGE_ALLOWED_PAIRS` gates
  every `Edge`; illegal `(from_type, to_type)` pairs raise at Pydantic
  validation, so no malformed edge reaches Neo4j.
