---
doc: ai-ta-backend/apollo/conversation/handlers/negotiate
description: apollo/handlers/negotiate.py — the P3 Negotiable-OLM challenge/paraphrase/skip/trace endpoints on a KG entry
owns:
  - apollo/handlers/negotiate.py
related:
  - ai-ta-backend/apollo/conversation/routing/router
  - ai-ta-backend/apollo/conversation/routing/errors
  - ai-ta-backend/apollo/knowledge-graph/store
last_verified: 2026-07-25
stub: false
---

# handlers/negotiate — P3 Negotiable-OLM moves

`apollo/handlers/negotiate.py` implements the three student "moves" on a
parser-authored KG entry plus a read-only trace, all scoped to the per-attempt
subgraph.

## Interface

- `handle_challenge(*, db, neo, session_id, entry_id, body)` — flag wrong/misheard
  → `store.mark_node_disputed`.
- `handle_paraphrase(...)` — student's preferred surface form (preserves
  structural fields, sets `student_belief`) → `store.paraphrase_node`.
- `handle_skip(...)` — pass to grader unchanged → `store.skip_node`.
- `handle_get_trace(...)` — node move history → `store.get_node_trace`.
- Request models `ChallengeRequest` (`reason`, ≤500 chars) and `ParaphraseRequest`
  (`surface_form`, ≤1000 chars) are exported and imported by `routing/router`;
  `SkipRequest` forbids extra fields. Helpers: `_resolve_active_attempt`,
  `_kg_snapshot`.

## Data flow

Each handler resolves the latest `ProblemAttempt` (`_resolve_active_attempt`;
`InvalidPhaseError` if none), applies one `KGStore` status write, then re-reads
the subgraph via `_kg_snapshot` and returns `{entry, kg, move}`.

## Invariants & gotchas

- **KG-native, no degrade**: a missing Neo4j client or a `KG_DEGRADED_ERRORS`
  member is re-raised as `KGUnavailableError` (503) rather than degrading to an
  empty graph. An entry absent from the per-attempt subgraph raises
  `KGEntryNotFoundError` (404).
- `handle_get_trace` now always returns an **empty moves list** — A6 deleted the
  `apollo_kg_negotiations` Postgres audit table. The three Neo4j status writes
  still work unconditionally (only the audit-row write was removed).

## Related

Move persistence: `knowledge-graph/store`; error semantics: `routing/errors`;
route wiring + request-model import: `routing/router`.
