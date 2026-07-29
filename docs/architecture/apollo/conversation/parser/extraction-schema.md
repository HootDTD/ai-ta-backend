---
doc: apollo/conversation/parser/extraction-schema
description: The strict OpenAI json_schema payload for the parser's one-call node + typed-edge extraction.
owns:
  - apollo/parser/extraction_schema.py
related:
  - apollo/conversation/parser/parser-llm
  - apollo/conversation/parser/edge-resolver
  - apollo/ontology/nodes
  - apollo/ontology/edges
last_verified: 2026-07-25
stub: false
---

`apollo/parser/extraction_schema.py` holds the parser's **output contract**.

## Interface

- `build_extraction_schema() -> dict` — returns the strict `json_schema` payload
  (`name: "kg_extraction"`, `strict: True`). Imported by `parser/parser-llm`. A
  fresh dict per call (callers never share/mutate a module-global schema).

## Data flow

The payload declares two arrays. `entries` items carry **flat, present-and-nullable**
per-type fields: `type` (enum of the 6 NodeTypes), `confidence`, `reuse_of`,
equation `symbolic`/`label`/`variables`, condition/simplification `applies_when`/
`transformation`, definition `concept`/`meaning`, variable_mapping `term`/`symbol`,
procedure_step `action`/`purpose`, and `uses_equation_ordinals` (nullable int array).
`edges` items carry `edge_type` (the `EdgeType` enum values), `from_ref`, `to_ref`,
and `provenance` (`explicit`|`inferred`). `parser_llm` feeds these to `_build_nodes`
and `edge_resolver`.

## Invariants & gotchas

- **Strict-mode rules the schema must honor:** `strict: true`,
  `additionalProperties: false` on every object, and **every** property listed in
  `required`. Optional-by-value fields are expressed as nullable types
  (`{"type": ["string", "null"]}`) — this is why all type-specific entry fields are
  present-and-nullable rather than omitted.
- **Keep in sync with the ontology's node/edge types and `EDGE_ALLOWED_PAIRS`** —
  this schema shapes what the LLM emits; edge *validity* is enforced downstream by
  `parser/edge-resolver`, not here.
- `uses_equation_ordinals` MUST stay in the schema — a field absent under
  `additionalProperties: false` can never arrive, and the deterministic within-turn
  USES fallback reads it. `_ENTRY_TYPES` ordering is kept stable for the offline
  strict-schema assertion test. Ported from the RQ3 spike `RESPONSE_SCHEMA`
  (reference-only, never imported).

## Related

Consumer `parser/parser-llm`; edge enforcement `parser/edge-resolver`; type
authorities `ontology/nodes`, `ontology/edges`.
