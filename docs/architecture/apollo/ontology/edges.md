---
doc: apollo/ontology/edges
description: The four-type KG edge taxonomy with typed endpoint constraints enforced at Pydantic construction.
owns:
  - apollo/ontology/edges.py
related:
  - apollo/ontology/nodes
  - apollo/ontology/graph
last_verified: 2026-07-25
stub: false
---

# Ontology — edges

The KG edge taxonomy: exactly **four** edge types, each with typed
`(from_node_type, to_node_type)` constraints checked when the `Edge` is built.

## Interface

Imported by the store, parser (`edge_resolver`, `extraction_schema`), overseer
(`topic_score`, `coverage`), solver (`sufficiency`), and provisioning
(`promotion_lint`):

- `Edge` — the Pydantic edge model.
- `EdgeType` — `StrEnum` of `PRECEDES` / `USES` / `DEPENDS_ON` / `SCOPES`.
- `EdgeProvenance` — `Literal["explicit", "inferred"]` (default `explicit`;
  only the LLM downgrades to `inferred`).
- `EDGE_ALLOWED_PAIRS` — `dict[EdgeType, set[(NodeType, NodeType)]]`, the
  authoritative legality map used by both the `Edge` validator and the parser.

## Data flow

`Edge` carries `edge_type`, `from_node_id`/`to_node_id`, `attempt_id`,
`source`, `provenance`, and the caller-resolved `from_node_type`/`to_node_type`.
The `@model_validator(mode="after")` rejects self-loops and any pair absent from
`EDGE_ALLOWED_PAIRS[edge_type]`. `PRECEDES` allows only
`procedure_step→procedure_step`; `USES` only `procedure_step→equation`;
`SCOPES` only `simplification|condition → equation`; `DEPENDS_ON` is generic
across all node-type pairs (self-type pairs included, self-node-id loops
excluded).

## Invariants & gotchas

- **`edge.attempt_id` is REQUIRED** so a `DETACH DELETE` on a subgraph's nodes
  cascades to its edges and per-attempt indexes work.
- Pair validation runs **at construction**, so an illegal edge never persists;
  when `from_node_type`/`to_node_type` are `None` (unresolved) the pair check is
  skipped and the store rejects the edge later as `unknown_endpoint_type`.
- `USES` carries procedure→equation links and `PRECEDES` carries procedure
  ordering — the two fields removed from `ProcedureStepNode` (see
  [nodes](nodes.md)).

## Related

- [ontology/nodes](nodes.md) — the node types these pairs constrain.
- [ontology/graph](graph.md) — traverses `PRECEDES`/`USES` over the aggregate.
