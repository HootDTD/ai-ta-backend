---
doc: apollo/conversation/routing/router
description: apollo/api.py FastAPI /apollo router, the process-singleton Neo4j client, and apollo-wide exception-handler registration
owns:
  - apollo/api.py
  - apollo/__init__.py
related:
  - apollo/conversation/routing/errors
  - apollo/conversation/routing/auth-deps
  - apollo/conversation/agent/persona-reply
  - apollo/persistence/neo4j-client
  - apollo/provisioning/_index
last_verified: 2026-08-11
stub: false
---

# routing/router — the /apollo APIRouter

`apollo/api.py` is the `APIRouter(prefix="/apollo")` mounted into `server.py`.
`apollo/__init__.py` is empty namespace glue that rides here (§4.0.7).

## Interface

- `router` — the `APIRouter`; `server.py` includes it and wires `close_neo4j_client` to shutdown.
- `get_neo4j_client() -> Neo4jClient | None` — process-singleton getter (FastAPI dep).
- `require_neo4j_client(...) -> Neo4jClient` — dep for KG-native routes; raises `KGUnavailableError` when the client is None.
- `close_neo4j_client()` — closes + clears the singleton (shutdown hook).
- `register_exception_handlers(app)` — installs every `apollo.errors` → JSON handler (incl. `EmptyAttemptError` → 409 `empty_attempt`, 2026-08-07).
- `GradingInProgressError` → 409 `grading_in_progress` (`grading_in_progress_handler`).
- Request models: `FromHootRequest`, `SessionCreateRequest`, `ChatRequest`,
  `NextRequest`. `ChatRequest` is `{message: str, ask_hoot: bool = false}`;
  omitting `ask_hoot` preserves the ordinary teaching-turn contract.

## Data flow

Route → handler → auth dep (owned by `routing/auth-deps`):

| Route | Handler | Auth dep |
|---|---|---|
| POST `/sessions/from_hoot` | `init_session_from_hoot` | user + course_member |
| POST `/sessions` | `init_session_direct` | user + course_member |
| GET `/sessions/{id}` | `handle_get_session` | session_owner |
| POST `/sessions/{id}/chat` | `handle_chat` (`message` + optional `ask_hoot`) | session_owner |
| POST `/sessions/{id}/done` | `handle_done` | session_owner |
| POST `/sessions/{id}/retry` | `handle_retry` | session_owner |
| POST `/sessions/{id}/next` | `handle_next` (lazy import) | session_owner |
| POST `/sessions/{id}/restart_problem` | `handle_restart_problem` (lazy) | session_owner |
| POST `/sessions/{id}/end` | `handle_end` | session_owner |
| GET `/progress` | `handle_get_progress_detail` | user + course_member |
| GET `/concepts` | `list_course_concepts` (inline) | user + course_member |
| GET `/problems` | `handle_list_problems` | user + course_member |
| POST `/sessions/{id}/kg/{entry}/{challenge,paraphrase,skip}`, GET `.../trace` | `handle_*` (negotiate) | session_owner |
| GET `/teacher/classroom/{id}/heatmap`,`/struggles` | `mastery_heatmap`/`struggle_signals` | course_teacher |
| GET `/teacher/classroom/{id}/performance` | `class_performance` (projections/performance) | course_teacher |

Session-scoped routes inject the auth dep only for its gate side-effect (401/403/404); the identity is unused at this layer. Teacher classroom routes are teacher-gated (they expose every student's state).

## Invariants & gotchas

- **Neo4j is a process singleton, degraded-first**: `get_neo4j_client` never raises — a construction failure logs and returns None, and **there is NO negative caching**, so the next request retries fresh. KG-touching handlers degrade explicitly on None.
- **`ContextOverflowError` is registered lazily**: `register_exception_handlers` imports it from `apollo.agent.apollo_llm` inside the function to avoid a top-level circular import — see `agent/persona-reply` (it is only raised inside dead code).
- The router `include_router`-mounts three **provisioning** routers (authored-sets, problem-generation, concepts) owned by `provisioning/_index` — no double-ownership of `api.py`.
- **Apollo-WIDE surface**: exception-handler registration covers grading/KG/provisioning error classes, and the Neo4j singleton serves every KG-touching handler across all apollo sub-domains — not conversation-only.

## Related

Error semantics live in `routing/errors`; auth deps in `routing/auth-deps`; the Neo4j driver in `persistence/neo4j-client`; the mounted teacher routers in `provisioning/_index`.
