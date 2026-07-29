---
doc: apollo/conversation/parser/edge-resolver
description: Maps LLM-emitted edge refs to validated typed Edges, enforcing endpoint rules at the parser output boundary.
owns:
  - apollo/parser/edge_resolver.py
related:
  - apollo/conversation/parser/parser-llm
  - apollo/conversation/parser/graph-context
  - apollo/conversation/parser/extraction-schema
  - apollo/ontology/edges
last_verified: 2026-07-25
stub: false
---

`apollo/parser/edge_resolver.py` (WU-2A) is pure logic — no LLM, no I/O.

## Interface

- `resolve_typed_edges(raw_edges, *, index_to_node, graph_context, attempt_id) -> list[Edge]`
  — imported by `parser/parser-llm`. Rejected edges are dropped + logged, never
  raised.

## Data flow

Each LLM edge (`from_ref`/`to_ref`/`edge_type`/`provenance`) becomes a validated
typed `Edge`. `_resolve_ref` resolves a `^n\d+$` **this-response ordinal** against
`index_to_node[i]` (the ORIGINAL entry index), or a bare id with `graph_context`
against that context node (a cross-turn endpoint — the id is returned even when its
type is unknown so the caller can tell "unresolvable" from "type unknown").
`_build_typed_edge` rejects, in order, `unresolvable_ref`, `unknown_endpoint_type`,
`self_loop`, `bad_edge_type`, and `disallowed_pair` (an `EDGE_ALLOWED_PAIRS`
pre-check) **before** constructing the `Edge`. `_coerce_provenance` yields
`"inferred"` only when the raw value is `"inferred"`, else `"explicit"`. `_reject`
logs `parser_edge_rejected` with a reason.

## Invariants & gotchas

- **The §6.3 endpoint rules (`EDGE_ALLOWED_PAIRS`) are enforced here, at the parser
  OUTPUT boundary** — a structurally illegal pair is dropped at this seam, not
  downstream.
- The ordinal namespace (`^n\d+$`) and `graph_context` ids are **disjoint by
  construction** (`graph_context.is_safe_context_id` guarantees it); an out-of-range
  ordinal returns unresolvable and does NOT fall through to `graph_context`.
- No silent drops: malformed (non-dict) raw edges are also logged and skipped. A
  final `Edge`-validator `ValueError` is caught as `validator_rejected` belt-and-braces.

## Related

Caller `parser/parser-llm`; cross-turn ids `parser/graph-context`; emitted shape
`parser/extraction-schema`; `Edge`/`EdgeType`/`EDGE_ALLOWED_PAIRS` authority
`ontology/edges`.
