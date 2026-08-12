---
doc: apollo/knowledge-graph/store
description: KGStore — Neo4j-backed per-attempt subgraph CRUD plus Postgres-owned freeze enforcement; the handler-facing KG seam.
owns:
  - apollo/knowledge_graph/store.py
  - apollo/knowledge_graph/__init__.py
related:
  - apollo/ontology/graph
  - apollo/solver/sympy-exec
  - apollo/knowledge-graph/canon-projection
  - apollo/persistence/neo4j-client
  - apollo/conversation/handlers/done
  - apollo/conversation/handlers/chat
  - apollo/conversation/handlers/negotiate
last_verified: 2026-08-12
stub: false
---

# Knowledge-graph — store

`KGStore` is the per-request seam the conversation handlers use for all KG work:
Neo4j owns the graph, Postgres owns session freeze state. One cohesive ~812-line
class; `__init__.py` is empty namespace glue. Constructed by
`conversation/routing/router.md::get_kg_store` with `(db: AsyncSession,
neo: Neo4jClient | None)`. Callers: `handlers/{chat,done,lifecycle,negotiate,
restart_problem}` and `provisioning/authored_sets/api`.

## Interface

Method catalog, grouped by concern (all `async` unless noted):

**Degrade guard**
- `__init__(db, neo)` · `_require_neo(*, stage)` — raises `KGUnavailableError`
  when `neo is None` (see [_index](_index.md) degraded invariant).

**Postgres freeze / metadata**
- `freeze(session_id)` / `unfreeze(session_id)` — flip `TutoringSession.phase`
  (`PROBLEM_REVEAL` ↔ `TEACHING`), commit. **Done no longer calls `freeze`**
  (M1/P3.4): its blind read-modify-write left the session in `PROBLEM_REVEAL`,
  which `restart_problem._FROZEN_PHASES` does NOT cover, so a restart could slip
  in and wipe the transcript mid-grade. `handlers/done`'s single CAS claim
  writes `SOLVING` directly and provides the same `_ensure_unfrozen` guarantee.
- `_session_id_for_attempt(attempt_id)` · `_ensure_unfrozen(session_id)` —
  raise `SessionFrozenError` when phase is a frozen state.

**Neo4j writes**
- `write_nodes(*, attempt_id, nodes, source, user_id=None, search_space_id=None)`
  → count actually created (cross-turn dedup; WU-3C1 scoping/timestamp stamping).
- `write_edges(*, attempt_id, edges, source)` → `WriteEdgesResult`
  (`written`/`dropped`/`invalid` + per-rejection `reasons`; `int()`-coercible).
- `_existing_node_ids(s, attempt_id, ids)` — one scoped read shared with writes.

**Neo4j reads**
- `read_graph(*, attempt_id)` → `KGGraph`.
- `read_node_created_at(*, attempt_id)` / `read_node_graded_at(*, attempt_id)` →
  `{node_id: iso}` — expose the metadata `read_graph` strips.
- `walk_chain(*, attempt_id, start_node_id, edge_types, max_depth=20)`.

**Lifecycle / retention**
- `delete_subgraph(*, attempt_id)` — idempotent `DETACH DELETE`.
- `stamp_graded_at(*, attempt_id, ts=None)` → count — Done-time freeze stamp.

**Apollo summary**
- `summarize_for_apollo(*, attempt_id)` → bullet string for Apollo's context.

**Negotiable OLM (P3)**
- `mark_node_disputed(...)` / `paraphrase_node(...)` / `skip_node(...)` → `Node`
  (status → `DISPUTED` / `DUAL`), via `_set_node_status_neo4j`.
- `get_node_trace(*, attempt_id, node_id)` → `{node_id, moves, source_utterance}`.

Module-level: `WriteEdgesResult` (frozen), `_node_to_neo4j_props`,
`_record_to_node`, `_equation_latex`, the per-label `_NODE_CREATE_CYPHER` /
`_EDGE_CREATE_CYPHER` template maps.

## Data flow

Writes flow `Node`/`Edge` → property bag → per-label `CREATE` (Cypher forbids
dynamic labels, so one template per label, each applying the type label + the
secondary `:_KGNode` label). Reads flow Neo4j record → `_record_to_node` →
`KGGraph`. `_ensure_unfrozen` gates every mutation. Equation display LaTeX is
rendered via `solver.sympy_exec.parse_zero_form` + `_tidy_floats`
(`_equation_latex`). At Done, `handlers/done.md` claims (CAS) → `read_graph`
→ (grade) → `stamp_graded_at`.

## Invariants & gotchas

- **Scoping.** Every subgraph is keyed by the `attempt_id` property + the
  `:_KGNode` label; all reads/writes go through `KGGraph`.
- **Cross-turn dedup (fair coverage).** `write_nodes` REUSES nodes whose id
  already exists rather than re-CREATEing (which would clone a node and inflate
  coverage); the return counts only genuinely new nodes (`kg_entries_added`).
- **Edge rejections are DATA, not exceptions.** `write_edges` validates before
  any `CREATE`: an absent endpoint is `dropped` (`endpoint_absent`), an
  unknown/`EDGE_ALLOWED_PAIRS`-violating pair is `invalid`; both are logged
  (`write_edge_rejected`) and never silently lost.
- **`read_graph` strips metadata.** `user_id`, `search_space_id`, `created_at`,
  `graded_at`, and the four WU-3C2 resolution fields are node metadata, popped
  before content reconstruction so reads round-trip byte-identically; the two
  timestamp helpers expose `created_at`/`graded_at` separately.
- **Retention.** `delete_subgraph` is NO LONGER called by `handle_end`
  (subgraphs persist, WU-3C1); it remains the janitor's pruning primitive and
  `restart_problem`'s explicit student wipe.
- **`stamp_graded_at` is NO-FALLBACK.** Done-time, idempotent, Neo4j-only (there
  is no Postgres `graded_at` column); a failure raises `RetentionError` — loud
  but never voids the already-committed grade; the optional `ts` threads one
  captured `done_ts` so Neo4j `graded_at` and Postgres `last_evidence_at` carry
  the identical instant.
- **A6: `get_node_trace` always returns empty `moves`.** The
  `apollo_kg_negotiations` Postgres audit table was deleted (DB-13/A6); the
  Done-gate reads `status` from the graph directly. `source_utterance` is the
  latest student message in the attempt (approximate). A stale docstring
  reference to `done_turn_order` is the only remaining mention of that dead
  module.

## Related

- [ontology/graph](../ontology/graph.md) — the `KGGraph` shape all I/O uses.
- [solver/sympy-exec](../solver/sympy-exec.md) — equation LaTeX rendering.
- [knowledge-graph/canon-projection](canon-projection.md) — the sibling `:Canon`
  seeder.
- [persistence/neo4j-client](../persistence/neo4j-client.md) — the `Neo4jClient`
  session source.
- [conversation/handlers/done](../conversation/handlers/done.md),
  [chat](../conversation/handlers/chat.md),
  [negotiate](../conversation/handlers/negotiate.md) — the live callers.
