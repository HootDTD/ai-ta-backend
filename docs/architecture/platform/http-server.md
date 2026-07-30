---
doc: platform/http-server
description: server.py — the FastAPI composition root and Hoot QA HTTP surface (/ask, teacher, invite-links), plus the teacher retrieval-weights tuning seam
owns:
  - server.py
related:
  - platform/auth
  - platform/config-contracts
  - platform/config-weights
  - platform/workspaces
  - rag-pipeline/retrieve-pipeline
  - rag-pipeline/main-ai
  - rag-pipeline/store-bias
  - rag-pipeline/citations-formatter
  - apollo/conversation/routing/router
  - reports/ai-use-routes
  - chats/routes
  - knowledge/teacher-weekly
last_verified: 2026-07-30
stub: false
---

# platform/http-server — composition root + QA HTTP surface

`server.py` is the single FastAPI app. **Known monolith (R2, ~2113 lines):** one
doc must own the whole file today (bijection); a route-group code-split into
`routers/*.py` is the follow-up that would let this doc fan out.

## Interface

- `app = FastAPI(title="AI-TA HTTP Server")` — created at import; the apollo
  router is mounted and its exception handlers registered immediately.
- Startup hook `_validate_startup_env()` → `auth.validate_required_env()`.
- Route table (auth tier: **P**ublic / **A**uthenticated / **M**ember /
  **T**eacher):

| Route | Tier | Notes |
|---|---|---|
| `GET /healthz` | P | liveness |
| `GET /materials/file-url` | M | `?upload_id=` (citations' `teacher_upload_id`) or `?doc_id=` (review pointers) → 5-min signed URL for the private-bucket source PDF; 404 when the material has no `storage_key`, 502 on sign failure |
| `POST /ask`, `POST /ask/stream` | M | **sync `def` by design**; auto-enrolls student |
| `GET /classes` | P | catalog list |
| `POST /classes`, `GET /my-classes` | A | |
| `GET /teacher/weeks`, `POST /teacher/weeks/current` | T | week config |
| `GET/POST /teacher/retrieval-weights` | T | reads/writes per-course bias |
| `POST /teacher/upload` | T | 202; `python-multipart` optional → **503** stub if absent |
| `POST /teacher/uploads/{id}/retry` | T | 202 requeue |
| `POST /invite-links`, `GET /invite-links`, `DELETE /invite-links/{id}` | T | CRUD (delete 204) |
| `GET /invite-links/resolve/{code}` | P | preview |
| `POST /invite-links/redeem/{code}` | A | enroll self |

- QA-pipeline glue this file **owns** (orchestration only — internals live in
  rag-pipeline): `_require_course_membership`/`_resolve_request_auth`,
  `_ask_pgvector`, `_prepare_router_context_sync`, `_retrieve_bundle_with_router`,
  `_persist_router_outcome_sync`, `_load_memory_and_append_user_turn`,
  `_append_assistant_turn_and_refresh`, `_structured_citations_from_bundle`,
  `_keywords_from_bundle`, `_save_attachments`, and the pydantic request/response
  models (`AskRequest`, `UploadOut`, `Teacher*Out`, `InviteLink*`).
- **`_build_retrieval_weight_overrides`** — the live teacher retrieval-tuning
  seam (§4.0.15): starts from `get_env_weights()`, layers workspace
  `weight_overrides`, then per-material overrides, then the teacher's saved
  `get_retrieval_weights_by_search_space` — the merged dict feeds `store-bias`.

## Data flow

`POST /ask` → `_require_course_membership` (auth) → resolve `ClassWorkspace`
(`workspaces`) → `_build_retrieval_weight_overrides` → `_ask_pgvector`
(`retrieve_for_question` in `rag-pipeline/retrieve-pipeline`) →
`solve_with_bundle`/`format_answer` (`rag-pipeline/main-ai`) → `format_citations`
(`citations-formatter`) → persist turn + refresh memory (`chats`). The bundle
payload is a `config.contracts.ResearchBundle` — see `config-contracts`.

## Invariants & gotchas

- **`/ask` MUST stay sync `def`** — an in-file comment forbids `async def` unless
  the whole pipeline is made async (FastAPI auto-threads sync endpoints; the
  asyncpg loop is bridged via `run_async`).
- **Auth is per-endpoint, not middleware** — each route calls
  `_require_course_membership`/`resolve_auth_context` itself.
- **`search_space_id` is the canonical course key**; a request carrying legacy
  `class`/`doc_sets` is rejected (400).
- **Router mounts it does NOT own:** `apollo_router` is always mounted;
  `reports_router` and `chats_router` are mounted inside `try/except` so a broken
  optional import can't kill boot. Each router is owned by its domain doc.
- One `CORSMiddleware`, origins from `CORS_ALLOW_ORIGINS` (default `*`).
- Wire-log lines ride the response via `redirect_stdout` capture (gated by
  `RETRIEVAL_WIRE_LOG`); 500 bodies expose detail only under `DEBUG_HTTP_ERRORS`.
- The `__main__` block (`uvicorn.run("backend.server:app", …)`) is **stale** — it
  names a `backend.` package prefix that prod never uses; prod runs
  `uvicorn server:app` per the Procfile.

## Env flags

`CORS_ALLOW_ORIGINS`, `RETRIEVAL_WIRE_LOG`, `DEBUG_HTTP_ERRORS`, `TOKEN_BUDGET`,
`K_SEM`, `PORT`. (Auth/embedding/weight flags are owned by `auth`,
`config-settings`, `config-weights`.)

## Related

`store-bias` + `config-weights` complete the retrieval-weights recipe (§4.0.15);
`apollo/conversation/routing/router`, `reports/ai-use-routes`, `chats/routes` own
the three mounted routers.
