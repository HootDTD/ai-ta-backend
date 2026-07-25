---
doc: apollo/ontology/nodes
description: The six-node KG taxonomy — a Pydantic discriminated union plus the build_node factory and label maps.
owns:
  - apollo/ontology/nodes.py
related:
  - apollo/ontology/edges
  - apollo/ontology/graph
  - apollo/resolution/content-tiers
  - apollo/knowledge-graph/store
last_verified: 2026-07-25
stub: false
---

# Ontology — nodes

The KG node taxonomy: **six** node types, one per KG entry kind, modelled as a
Pydantic **discriminated union** over a typed `content` payload.

## Interface

Imported widely (parser, resolution, store, provisioning schema, overseer):

- `Node` — `Annotated[... , discriminator="node_type"]` union of the six
  concrete node classes.
- `NodeType` — `Literal` of the six type strings (`equation`, `condition`,
  `simplification`, `definition`, `variable_mapping`, `procedure_step`).
- `NodeSource` — provenance `Literal` (`parser` / `reference` / `system`).
- Concrete `*Node` classes (`EquationNode`, …) and their `*Content` payloads
  (`EquationContent`, `ConditionContent`, …).
- `build_node(*, node_type, node_id, attempt_id, source, content, …)` — the
  factory that maps a `(type, content_dict)` pair to a typed node.
- `NODE_LABELS` (type→Neo4j label), `NODE_LABEL_TO_TYPE` (reverse), and
  `NODE_CONTENT_TYPES` (type→content class) module maps.

## Data flow

`_NodeBase` carries the fields common to every node: `node_id` (unique within an
attempt subgraph), `attempt_id` (subgraph scoping), `source`,
`parser_confidence` (default `1.0`), the Negotiable-OLM `status`
(`ACCEPTED`/`DISPUTED`/`DUAL`) + `student_belief`, and the F-struct `entity_key`.
Each concrete node pins `node_type` as a `Literal` and declares its own
`content` model. `build_node` is the one construction point the store and
seed converters call.

## Invariants & gotchas

- **Procedure-step `order` and `uses_equations` are GONE.** Ordering derives
  from `PRECEDES` edges; procedure→equation links are real `USES` edges (see
  [edges](edges.md)).
- `parser_confidence` defaults `1.0` so non-parser and pre-P3 legacy nodes stay
  authoritative; the P3 OLM Done-gate triggers on `< 0.6`.
- `status` defaults `ACCEPTED` and `student_belief`/`entity_key` default `None`
  so pre-negotiation and pre-F-struct nodes round-trip byte-identically.
- `entity_key` is populated on **reference** nodes only (`Problem.to_kg_graph`);
  parser/system/legacy nodes leave it `None`.
- `NODE_LABELS` values are applied alongside the secondary `:_KGNode` label so a
  single Neo4j index covers all subgraph reads (see
  [knowledge-graph/store](../knowledge-graph/store.md)).

## Related

- [ontology/edges](edges.md), [ontology/graph](graph.md) — the rest of the
  shape layer.
- [resolution/content-tiers](../resolution/content-tiers.md) — matches on the
  per-type surface text.
- [knowledge-graph/store](../knowledge-graph/store.md) — maps nodes to/from
  Neo4j property bags.
