---
doc: apollo/conversation/handlers/chat
description: apollo/handlers/chat.py — handle_chat, the full V3 teaching turn (intent gate, parse, KG write, questioning)
owns:
  - apollo/handlers/chat.py
  - apollo/handlers/__init__.py
related:
  - apollo/conversation/handlers/intent
  - apollo/conversation/handlers/done
  - apollo/conversation/parser/parser-llm
  - apollo/conversation/parser/graph-context
  - apollo/conversation/questioning/controller
  - apollo/conversation/curriculum/db
  - apollo/knowledge-graph/store
  - apollo/overseer/problem-selector
  - apollo/persistence/neo4j-client
last_verified: 2026-07-25
stub: false
---

# handlers/chat — the V3 teaching turn

`handle_chat` is `POST /apollo/sessions/{id}/chat`. `apollo/handlers/__init__.py`
is empty namespace glue riding here (§4.0.7).

## Interface

- `handle_chat(*, db, neo, session_id, message) -> dict` — the only public entry
  (called by `routing/router`). Returns `{apollo_reply, kg_entries_added, kg,
  covered_topics, question_target?}`, or an `intent_*` variant when the intent
  gate fires.

## Data flow

Ordered turn:

1. Load session + latest `ProblemAttempt` + `ConceptDefinition`
   (`load_concept_definition`, `curriculum/db`).
2. **Intent state machine** (`handlers/intent`): if `pending_intent == "done"`,
   `_handle_pending_done` treats this utterance as a confirmation — an affirmation
   dispatches `handle_done` (`handlers/done`, lazy import) and returns; any other
   pending intent is just cleared. Otherwise `_maybe_intent_confirmation`
   classifies the utterance and, above threshold, persists a confirmation prompt
   and returns.
3. **Teaching path**: read the current subgraph (`_read_graph_or_empty`), project
   it via `build_graph_context` (`parser/graph-context`), then `parse_utterance`
   (`parser/parser-llm`) → nodes/edges.
4. **KG write** (`_write_kg_or_skip`): `write_nodes`/`write_edges` on `KGStore`;
   returns genuinely-new node count.
5. **Questioning**: build the full transcript and call `plan_next_question`
   (`questioning/controller`), which produces Apollo's reply + `covered_topics`.
   When it decides `done`, `handle_done` is dispatched.
6. `_persist_turn` appends the atomic (student, apollo) pair; `turn_index` from
   `_next_turn_index`.

## Invariants & gotchas

- **`chat` has its OWN `_load_history`** (full per-attempt transcript, role-mapped
  student→user / apollo→assistant) — it does **not** use the vestigial
  `handlers/history`.
- **A parse miss still proceeds**: `ParserCouldNotExtractError` is caught, the
  turn contributes zero KG entries, and the conversational reply is generated
  anyway (no 422 card to the student).
- **Neo4j is optional**: every KG read/write degrades on `KG_DEGRADED_ERRORS`
  (`_read_graph_or_empty` → empty `KGGraph`; `_write_kg_or_skip` → `nodes_added=0`);
  the Postgres + LLM reply always ships.
- `_handle_pending_done` / the questioning `done` branch import `handle_done`
  lazily to break the `handle_done ← store ← chat` import cycle.

## Related

Intent gate: `handlers/intent`; grading dispatch: `handlers/done`; parse:
`parser/parser-llm` + `parser/graph-context`; reply: `questioning/controller`;
concept load: `curriculum/db`; KG: `knowledge-graph/store` +
`persistence/neo4j-client`; problem lookup: `overseer/problem-selector`.
