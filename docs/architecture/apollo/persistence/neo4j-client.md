---
doc: apollo/persistence/neo4j-client
description: The async Neo4j seam for Apollo's per-attempt KG store — driver wrapper, the degraded-mode error tuple, and the run-once Aura schema DDL.
owns:
  - apollo/persistence/neo4j_client.py
  - apollo/persistence/neo4j_schema.cypher
related:
  - apollo/persistence/_index
  - apollo/knowledge-graph/_index
  - apollo/knowledge-graph/canon-projection
  - apollo/conversation/routing/router
last_verified: 2026-07-25
stub: false
---

# apollo/persistence/neo4j-client

The async Neo4j seam every Apollo KG read/write goes through.

## Interface

- **`Neo4jClient`** — one instance per FastAPI process. `from_env()` reads
  `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` from the
  environment **only** (never hardcoded); `session()` is an async context manager
  yielding a fresh per-request `AsyncSession`; `healthcheck()` runs `RETURN 1`;
  `close()` shuts the driver.
- **`KG_DEGRADED_ERRORS`** — the exact infra-failure exception tuple
  (`KGUnavailableError`, `DriverError`, `AuthError`, `TransientError`,
  `DatabaseUnavailable`, `OSError`, `asyncio.TimeoutError`) that triggers
  degraded-mode graph skips.
- **`neo4j_schema.cypher`** — run-once Aura DDL: per-label `(attempt_id, node_id)`
  uniqueness constraints for the 6 KG node labels; the shared `:_KGNode`
  secondary-label indexes on `attempt_id` and `user_id`; per-edge-type `attempt_id`
  relationship indexes (`PRECEDES`/`USES`/`DEPENDS_ON`/`SCOPES`, enabling cascade
  `DETACH DELETE`); and the `:Canon` `key`-unique constraint (makes
  `MERGE (c:Canon {key})` an upsert) + a course-scoped `search_space_id` index.

## Data flow

Consumed by `apollo/api.py`, the chat/done handlers, `knowledge_graph/{store,
canon_projection, resolution_store}`, and `provisioning/authored_sets/api.py`.
The process-singleton client is constructed at app startup and returns `None` on
failure so the app runs Neo4j-degraded (see `apollo/conversation/routing/router`).

## Invariants & gotchas

- **Env-only credentials** — `from_env` is the only constructor path in prod.
- **Degraded mode is infra-only.** `KG_DEGRADED_ERRORS` names the specific
  driver/transient/OS branches on purpose: the only common ancestor below
  `Exception` is `GqlError`, which also covers `CypherSyntaxError` — so a
  data-shape or Cypher bug must **NOT** be swallowed as "Neo4j unavailable".
- `:Canon` nodes are **projected from Postgres** `app.learner_entities` by
  `knowledge_graph/canon_projection.py`; the constraint here is the DB-level
  defense-in-depth behind that application-side `MERGE`.

## Env flags

`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`.

## Related

`apollo/knowledge-graph/canon-projection` (writes `:Canon`),
`apollo/knowledge-graph/_index` (the KG store that uses these sessions),
`apollo/conversation/routing/router` (constructs the process singleton).
