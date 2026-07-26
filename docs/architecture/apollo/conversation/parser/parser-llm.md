---
doc: apollo/conversation/parser/parser-llm
description: Live parser entry — a student utterance becomes typed Nodes + Edges via one GPT-4o structured call.
owns:
  - apollo/parser/parser_llm.py
  - apollo/parser/prompt_builder.py
  - apollo/parser/__init__.py
related:
  - apollo/conversation/parser/extraction-schema
  - apollo/conversation/parser/edge-resolver
  - apollo/conversation/parser/graph-context
  - apollo/conversation/curriculum/registry
  - apollo/conversation/curriculum/db
  - apollo/conversation/agent/llm-client
  - apollo/conversation/handlers/chat
  - apollo/ontology/nodes
  - apollo/ontology/edges
last_verified: 2026-07-25
stub: false
---

`apollo/parser/parser_llm.py` is the live parser entry point (`parser/__init__.py`
is empty glue).

## Interface

- `parse_utterance(utterance, *, concept: ConceptDefinition, attempt_id, graph_context=None, model=None) -> tuple[list[Node], list[Edge]]`
  — imported by `handlers/chat`.
- `prompt_builder.build_system_prompt(concept, *, concept_name=None) -> str`
  — imported by `parser_llm` only; substitutes `{{concept_name}}` in the concept's
  `parser_prompt_template.md`.

## Data flow

1. `_call_extraction`: **one** strict-`json_schema` GPT-4o call via a **direct
   `OpenAI()` client** at `model=` arg or `config.models.MAIN_MODEL`;
   `response_format` = `extraction-schema.build_extraction_schema()`; system prompt
   = `build_system_prompt(concept)`; user message = `_render_graph_context` EXISTING
   GRAPH block + CURRENT MESSAGE.
2. `_build_nodes`: `_entry_to_node`/`_flat_content` lift flat entries into typed
   `Node`s via `ontology.build_node`; `index_to_node` keys on the **ORIGINAL** entry
   index so a dropped malformed entry never shifts edge refs.
3. Edges: `edge-resolver.resolve_typed_edges`. No-context fallback (only when
   `graph_context is None` AND the model emitted no usable edges): deterministic
   `_resolve_uses_edges` (`uses_equation_ordinals` → USES) + `_build_precedes_chain`
   (consecutive `procedure_step` → PRECEDES).
4. Triviality gate `_is_non_trivial`: length floor (<10), `_TRIVIAL_ACKS`,
   `_EQUATION_LIKE` regex, else `_classify_teaching` (a `cheap_chat` LLM classifier
   at confidence ≥ 0.6). Raises `ParserCouldNotExtractError` when a non-trivial
   utterance yields zero entries (or JSON fails to decode); trivial/ack utterances
   return `([], [])` silently.

## Invariants & gotchas

- **v1 (diff-at-Done): the parser captures the student's OWN surface form** —
  canonical-symbol/subscript normalization is deferred to the Done-time diff (the
  template no longer substitutes symbol slots).
- **Two LLM tiers, not one:** the extraction call uses a direct client at MAIN_MODEL;
  only the triviality classifier uses `cheap_chat`. (Mapper drift: it is not a
  single `cheap_chat` extraction.)
- A parse miss contributes no nodes but the chat turn still proceeds; per-entry
  confidence defaults to 1.0; malformed entries are skipped, never raised mid-build.

## Env flags

- `APOLLO_CHEAP_MODEL` — triviality classifier tier. The extraction model is the
  `MAIN_MODEL` pin (`platform/config-model-pins`) or an explicit `model=` override.

## Related

Output contract `parser/extraction-schema`; edge validation `parser/edge-resolver`;
cross-turn context `parser/graph-context`; concept source
`curriculum/registry` + `curriculum/db`; caller `handlers/chat`; types
`ontology/nodes`, `ontology/edges`.
