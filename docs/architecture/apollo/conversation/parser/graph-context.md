---
doc: apollo/conversation/parser/graph-context
description: Builds the minimal read-only prior-attempt graph the parser needs for cross-turn edge linking.
owns:
  - apollo/parser/graph_context.py
related:
  - apollo/conversation/parser/parser-llm
  - apollo/conversation/parser/edge-resolver
  - apollo/knowledge-graph/store
  - apollo/ontology/graph
last_verified: 2026-07-25
stub: false
---

`apollo/parser/graph_context.py` (WU-2A) supplies optional prior-attempt context.

## Interface

- `build_graph_context(graph: KGGraph) -> GraphContext` — imported by
  `handlers/chat`.
- `GraphContext` (frozen dataclass; `is_empty()`, `type_of(node_id)`) — imported by
  `parser/parser-llm` and `parser/edge-resolver`. `ContextNode` is its element.
- `is_safe_context_id(node_id) -> bool`.

## Data flow

`build_graph_context` projects a read `KGGraph` into the minimal context the
parser's LLM call needs to reference earlier-turn nodes: a stable id, node type,
and a short label per node. `_label_for` derives the label deterministically per
node type (no LLM), truncated to 60 chars to mirror the prompt's `[:60]` rendering.
Any node whose id is NOT context-safe (would collide with a `^n\d+$` ordinal) is
skipped and logged (`graph_context_skip`), never silently kept. Returns a NEW frozen
`GraphContext` with an immutable tuple of nodes.

## Invariants & gotchas

- **Read-only:** this is context passed INTO the parser; it never mutates the KG.
- `is_safe_context_id` is coordinated with `edge_resolver._resolve_ref`'s ordinal
  shape — a context id must never look like `n<i>`, so the two ref namespaces stay
  disjoint.
- `type_of` lets `edge_resolver` enforce `EDGE_ALLOWED_PAIRS` for cross-turn
  endpoints whose type is absent from the current LLM response.
- Frozen dataclasses + tuple (not list) per the repo immutability rule.

## Related

Consumer `parser/parser-llm`; coordinated resolver `parser/edge-resolver`; source
of the `KGGraph` `knowledge-graph/store`; `KGGraph`/`NodeType` authority
`ontology/graph`.
