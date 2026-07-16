---
doc: ai-ta-backend/_overview
description: App bootstrap, HTTP surface, auth, config, vendor clients, and ops entrypoints for the Hoot FastAPI backend
owns:
  - server.py
  - auth.py
  - supabase_client.py
  - teacher_upload_worker.py
  - config/**
  - runtime/**
  - vendors/**
  - scripts/**
  - citations/**
  - workspaces/**
  - text-embeder/**
  - Procfile
  - pytest.ini
  - .coveragerc
  - requirements.txt
  - .pre-commit-config.yaml
  - .github/**
related:
  - shared/conventions
  - shared/security
  - shared/supabase
last_verified: 2026-07-16
stub: false
---

# ai-ta-backend overview

Hoot is a Python/FastAPI RAG teaching assistant. `server.py` is a single ~2000-line module that owns the FastAPI app, all top-level routes, and the QA request pipeline glue. Everything else in this doc's scope is supporting infrastructure: auth, per-request config, course-workspace resolution, vendor REST clients, citation formatting, and ops scripts.

**Doc tree:** sibling docs own the deep subsystems — `rag-pipeline.md` (ai/, retrieval/), `indexing.md` (indexing/, ocr/, text-embeder internals), `apollo.md` (apollo/), `domain-data.md` (database/, chats/, knowledge/, reports/). Do not look here for those.

## Module map and file landmarks

| Path | Role |
|---|---|
| `server.py` | FastAPI app, CORS, router mounting, all `/ask`, `/teacher/*`, `/knowledge/*`, `/invite-links*`, `/classes` routes. ~1970 lines. |
| `auth.py` | Supabase JWT validation (REST call to `auth/v1/user`), in-memory token cache, course-membership checks, student auto-enroll. |
| `supabase_client.py` | One-line backward-compat shim: `from vendors.supabase_client import *`. |
| `teacher_upload_worker.py` | Procfile `worker` entrypoint: `TeacherWeeklyStorage().run_upload_worker_loop()` — drains the queued teacher-upload ingestion jobs. |
| `Procfile` | `web: uvicorn server:app` + `worker: python -m teacher_upload_worker` + `apollo-janitor: python -m apollo.learner_janitor_worker` + `apollo-provision: python -m apollo.provision_worker` (Railway deploys web+worker; **`apollo-janitor` is scaled to 0 replicas until `APOLLO_LEARNER_JANITOR_ENABLED` is flipped** — WU-5B3b; **`apollo-provision` is scaled to 0 replicas until `APOLLO_AUTOPROVISION_ENABLED` is flipped** — WU-3B2g). |

### config/
| File | Role |
|---|---|
| `config/settings.py` | `RequestConfig` (per-request subject/citation-label/runtime-dir, replaces legacy module globals), subject-name precedence (`default < meta < env < cli/server`), `get_runtime_dir()`, pgvector flags (`use_pgvector_retrieval()`, `get_embedding_dim()` default 3072, `get_embedding_model()` default `text-embedding-3-large`), `get_supabase_db_url()`, Neo4j env getters + `neo4j_configured()`, reranker flags. |
| `config/weights.py` | Retrieval store-kind bias weights. `WEIGHT_KINDS = (textbook, slides, notes, homework, exams, other)`, env prefix `RETRIEVAL_STORE_WEIGHT_*`, defaults (textbook 0.12 … other 0.03), clamp to [0.0, 1.0], `get_env_weights()` / `normalize_weights()`. |
| `config/contracts.py` | Dataclass contracts for the QA pipeline: `ParsedTask`, `BundleSnippet`, `ResearchBundle`, `ResearchMetadata`. |

### vendors/
| File | Role |
|---|---|
| `vendors/supabase_client.py` | Thin PostgREST helpers (`select`, `select_one`, `insert`, `upsert`, `update`, `delete`, `rpc`) using `SUPABASE_URL` + anon `SUPABASE_API_KEY`; RLS enforces access server-side. |
| `vendors/supabase_storage.py` | `SupabaseStorageClient.upload_bytes/download_bytes/ensure_bucket` against Storage REST; prefers `SUPABASE_SERVICE_ROLE_KEY`, falls back to API/anon key. `ensure_bucket` POSTs `/storage/v1/bucket` (private by default) and tolerates already-exists (400/409 duplicate) — new environments no longer need manual bucket creation. |
| `vendors/openai_client.py` | OpenAI Chat Completions wrapper used by AI-use reports: `generate_ai_use_markdown(evidence_pack, style, length)` with token budgeting, retry/backoff, `REPORTS_MODEL` (default `gpt-4o-mini`), and a fake mode when `TEST_FAKE_OPENAI=1` or no API key. |

### OCR/env surface

- `APOLLO_UNIFIED_QUESTION_DEBUG_LOG` enables a default-off staging diagnostic containing bounded
  rejected/redrafted Apollo question text; production must keep it off because drafts can contain
  private rubric vocabulary. The behavior and logging boundary are documented in `apollo.md`.
- `OCR_PROVIDER=openai` selects the OpenAI vision OCR provider via `ocr/factory.py` for authored-set indexing paths that pass a provider into `TeacherPDFIngestor`; `OCR_PROVIDER=mathpix` keeps the existing Mathpix factory option.
- `APOLLO_OCR_MODEL` optionally overrides the OpenAI vision OCR model used by `OpenAIVisionOCRProvider.from_env()`; the code default is `gpt-4o`.
- `APOLLO_AUTHORED_OCR_CONF_THRESHOLD` is the authored-set low-confidence OCR threshold used by `run_authored_set_provisioning` when deciding whether an extracted reference needs generated-reference comparison; the code default is `0.6`.

### Other scoped dirs
| Path | Role |
|---|---|
| `workspaces/manager.py` | `ClassWorkspace` / `WorkspaceMaterial` dataclasses, `WorkspaceManager` (TTL cache, `CLASS_WORKSPACE_CACHE_TTL` default 300s), `StaticWorkspaceRepository` legacy fallback, `build_workspace_manager()` factory. |
| `workspaces/db.py` | `DBWorkspaceRepository` — the primary repo. Resolves identifier by slug, then case-insensitive name, then integer id against `aita_search_spaces`; materials come from `aita_documents` rows with `status.state == 'ready'`. `index_path` is empty in the pgvector path (no FAISS dirs). |
| `citations/formatter.py` | `build_citation_info()` + `format_citations()`: maps snippets to labels like `[Notes, Week 3, p. 12]`, dedupes by `(doc_type, file, page)`, marks `verified=True` only for Textbook sources. |
| `scripts/` | One-shot tools: `migrate_indexes_to_supabase.py` (legacy FAISS/SQLite → pgvector), `seed_apollo_concept_registry.py` (filesystem concept registry → `apollo_*` tables, idempotent), `seed_apollo_learner_model.py` (course-scoped, idempotent Apollo Layer-1 seeder — writes migration-026 `apollo_kg_entities`/`apollo_entity_prereqs` rows + annotates `apollo_concept_problems.payload` with reference-node entity links + declared solution paths; layers on top of the concept registry, WU-3B), `seed_apollo_misconceptions.py` (2026-07-02: course-scoped, idempotent seeder for the `apollo_misconceptions` TABLE bank — migration 019, DISTINCT from the `kind='misconception'` KG entities `seed_apollo_learner_model.py` mints; converts each concept's `misconceptions.json` via `apollo/persistence/misconception_bank_seed.py`, embeds `description` via `embed_text` unless `--no-embeddings`, upserts through `apollo/overseer/misconception_bank.py::upsert_entry`; wired into `campaign.cast.teacher.provision_seeded`), `test_search.py` (pgvector hybrid-search smoke test). Not imported by the app. |
| `runtime/` | Runtime artifact dir (location overridable via `RUNTIME_DIR`). Holds `uploads/` (written per request by `/ask` attachments), `debug/`, `teacher_weekly/` worker scratch. Not code. |
| `text-embeder/` | Legacy standalone layout-aware multimodal PDF embedder (`layout_multimodal_embedder.py`, CLI, FAISS+SQLite-FTS5 output) plus a 38 MB sample aerodynamics PDF. Pre-pgvector era; also the default `CLASS_INDEX_ROOT` for the static workspace fallback. Still LIVE — `knowledge/manager.py::_load_layout_module` dynamically loads `layout_multimodal_embedder.py` (hyphenated dir, not a normal import) for the `POST /knowledge/materials` route; do not delete. |

## Public interfaces

### Routes defined in server.py
Auth legend: **T** = teacher membership required, **M** = any course membership (auto-enrolls students), **A** = authenticated only, **P** = public.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/healthz` | P | `{"status": "ok"}` |
| POST | `/ask` | M | Full QA pipeline, sync `def`, returns `{answer, logs, citations}` |
| POST | `/ask/stream` | M | Same pipeline as SSE: `status` / `answer` / `error` events |
| GET / POST | `/classes` | P / A | List all search spaces / create class (creator becomes teacher) |
| GET | `/my-classes` | A | Classes the caller is enrolled in |
| GET | `/knowledge/subjects` | P | `KnowledgeManager.list_subjects()` |
| GET / POST | `/knowledge/stores` | P | List / register a knowledge store by index path |
| POST | `/knowledge/materials` | P | Multipart PDF upload → embed (503 if `python-multipart` missing) |
| GET | `/teacher/weeks` | T | Weekly notes/slides upload state per course |
| POST | `/teacher/weeks/current` | T | Set current week (1..`TEACHER_TOTAL_WEEKS`, default 16) |
| GET / POST | `/teacher/retrieval-weights` | T | Per-course store-kind weight overrides (bounds 0..1) |
| POST | `/teacher/upload` | T | Multipart PDF, 202 — enqueues for the worker process |
| POST | `/teacher/uploads/{upload_id}/retry` | T | 202, re-enqueue a failed upload |
| POST / GET | `/invite-links` | T | Create (deactivates prior link for course+role) / list |
| DELETE | `/invite-links/{link_id}` | T | Revoke (sets `is_active=False`) |
| GET | `/invite-links/resolve/{code}` | P | Code → `{search_space_id, course_name, role}` |
| POST | `/invite-links/redeem/{code}` | A | Join course; student→teacher upgrade supported; idempotent |

Mounted routers (owned by sibling docs): `apollo.api.router` (prefix `/apollo`), `reports.ai_use.routes.router` and `chats.routes.router` (both unprefixed, mounted defensively inside try/except so a broken import doesn't kill boot).

Apollo's mounted router now also includes the teacher-gated WU-AAS authored-set
sub-router: `POST /apollo/authored-sets`, `GET /apollo/authored-sets`,
`GET /apollo/authored-sets/{set_id}`, and
`POST /apollo/authored-sets/{set_id}/problems/{problem_id}/approve`. These routes
live in `apollo/provisioning/authored_sets/api.py` and are documented in
`apollo.md`; they use `require_user`/`require_course_member`, multipart PDF
uploads, and FastAPI `BackgroundTasks`.

### auth.py exports
- `AuthContext` — frozen dataclass `(user_id: str, access_token: str)`.
- `resolve_auth_context(request: Request) -> AuthContext` — extracts the Bearer token, checks an in-memory SHA-256-keyed cache (TTL `AUTH_TOKEN_CACHE_TTL_SECONDS`, default 60s), otherwise validates via `GET {SUPABASE_URL}/auth/v1/user`. Raises 401.
- `has_membership(db_session, *, user_id, search_space_id, role=None) -> bool` — row check against `CourseMembership`.
- `auto_enroll_student_membership(db_session, *, user_id, search_space_id) -> bool` — inserts a student row when enabled; `IntegrityError` (already enrolled) counts as success.
- `can_auto_enroll_student(search_space_id) -> bool` — gated by `AUTO_ENROLL_STUDENT_MEMBERSHIP` (default on) and optional `AUTO_ENROLL_SEARCH_SPACE_IDS` allowlist.
- `validate_required_env() -> None` — raises `RuntimeError` if `SUPABASE_URL`, `SUPABASE_API_KEY`/`SUPABASE_ANON_KEY`, `SUPABASE_DB_URL`, or `OPENAI_API_KEY` is missing. Called from the FastAPI startup event.

## Main data flows

### Boot sequence (web process)
1. Railway/Procfile runs `uvicorn server:app`. Importing `server.py` loads `.env` (python-dotenv, with a hand-rolled fallback parser) and configures root logging.
2. `app = FastAPI(...)` is created at module import; the Apollo router and its exception handlers are registered immediately.
3. The `startup` event runs `validate_required_env()` — the process fails fast on missing Supabase/OpenAI env.
4. `CORSMiddleware` is added with origins from `CORS_ALLOW_ORIGINS` (comma-separated, default `*`). This is the only middleware; auth is per-endpoint, not middleware.
5. Reports and chats routers are mounted inside try/except (optional at boot).
6. `TeacherWeeklyStorage` and the `WorkspaceManager` are lazy module-level singletons created on first use, not at boot.

### /ask request lifecycle
0. Kill switch: `HOOT_QA_ENABLED=0` (`config/settings.py::hoot_qa_enabled`, default ON, read per-request) 403s before validation — Apollo-only deployments (the MGMT pilot's prod) close the Q&A surface at the HTTP boundary while teacher uploads/indexing/Apollo stay live. Tests: `tests/integration/test_hoot_qa_flag.py`.
1. Validate: non-empty `question` OR attachments; `chat_id` required; `doc_sets` overrides are rejected with 400 (deprecated).
2. `_require_course_membership()` → `resolve_auth_context()` (Bearer token → Supabase) then membership check on `search_space_id`; for student-level access, missing membership triggers auto-enroll. 403 otherwise.
3. Workspace resolution: `WorkspaceManager.get(str(search_space_id))` → `DBWorkspaceRepository` (404 `WorkspaceNotFound`, 500 on config errors). The workspace supplies `class_name`, `subject_name`, materials, and weight overrides.
4. Attachments (base64 data URLs) are decoded to `runtime/uploads/`; images go through `vision_transcribe()` + `extract_keywords()` and the result is appended to the effective question.
5. Chat memory: load/create the chat session, build memory context from `memory_summary` + recent turns, persist the user turn (all via `run_async` onto the shared background event loop). Memory context is prepended to the question. The per-turn workspace refresh MERGES into `session.meta` — never replaces it — because meta also carries the orchestrator's `bundle_cache` fingerprint; a blind overwrite here invalidated the session cache on every turn (fixed 2026-06-12).
5a. Retrieval-mode decision (`ROUTER_ENABLED=true` only): `_prepare_router_context_sync` classifies the raw question NONE/AUGMENT/FRESH using the session bundle cache + a gpt-4o-mini call (see `rag-pipeline.md` step 3a). `None`/FRESH = legacy path.
6. Weight overrides merge, lowest to highest precedence: env defaults → workspace overrides → per-material overrides → teacher-set weights from `TeacherWeeklyStorage`.
7. Retrieval (`_retrieve_bundle_with_router` → cache reuse, top-up merge, or legacy `_ask_pgvector` → `retrieval.pipeline.retrieve_for_question`, building a `ResearchBundle`) and `parse_question` run in parallel on a 2-worker `ThreadPoolExecutor`; retrieval stdout is captured via `redirect_stdout`.
8. `solve_with_bundle()` → `format_answer()` → `_structured_citations_from_bundle()` (only markers the LLM actually used).
9. Assistant turn + citations persisted (the assistant turn also writes write-only chat keywords — `bundle.found_terms` via `_keywords_from_bundle` — to `chat_turns.keywords` on both happy paths; the streaming error path and the user turn write `[]`. See `rag-pipeline.md` step 14 + `domain-data.md`); memory summary refreshed. When routed, `_persist_router_outcome_sync` saves the bundle to the session cache + writes a `chat_router_decisions` telemetry row (non-fatal on failure). Response: `{answer, logs, citations}` where `logs` is the captured `[Main AI`/`[Indexer AI` wire-log lines.

`/ask/stream` is the same pipeline as an `async def` SSE generator: blocking stages are pushed to `run_in_executor`, with `status` events (`vision`, `retrieving`, `analyzing`, `formatting`) before a final `answer` or `error` event.

### Teacher upload flow (web + worker)
1. `POST /teacher/upload` (teacher-gated, PDF-only) streams the file to a temp dir and calls `TeacherWeeklyStorage.enqueue_upload_by_search_space(...)` — returns 202 immediately.
2. The separate `worker` dyno (`teacher_upload_worker.py`) runs `run_upload_worker_loop()`, which performs OCR/indexing for queued uploads (details in `indexing.md` / `domain-data.md`).
3. `POST /teacher/uploads/{id}/retry` re-enqueues individual failed uploads (the former `reset_pending.py` bulk-reset script — an unimported one-off — was removed as junk; 2026-07-16 cleanup).

### apollo-janitor flow (third, dormant process)
The Procfile's third process — `apollo-janitor: python -m apollo.learner_janitor_worker` (`apollo/learner_janitor_worker.py`, owned by `apollo.md`) — is a dormant async-native learner-update retry janitor: a single `asyncio.run(main())` poll loop that, when `APOLLO_LEARNER_JANITOR_ENABLED` is ON (default OFF EVERYWHERE), drains pending Apollo Layer-3 belief updates one row per pass via the frozen `apollo.handlers.learner_janitor.drain_pending_attempts`, with cooperative SIGTERM/SIGINT shutdown between drains. It is **scaled to 0 replicas while dormant** so it costs no replica; activation is a HUMAN deploy step — flip `APOLLO_LEARNER_JANITOR_ENABLED` (and also `APOLLO_GRAPH_SIM_LAYER3_ENABLED` for belief writes, else the drain re-runs the shadow and DEFERS the belief write) then scale to 1. See `apollo.md` for the worker internals and `2026-06-19-apollo-kg-wu5b3b-janitor-worker-plan.md` §13 for the deploy runbook.

### apollo-provision flow (fourth, dormant process)
The Procfile's fourth process — `apollo-provision: python -m apollo.provision_worker` (`apollo/provision_worker.py`, owned by `apollo.md`) — is the dormant §8B auto-provisioning worker (WU-3B2g): a single `asyncio.run(main())` poll loop MIRRORING the janitor that, when `APOLLO_AUTOPROVISION_ENABLED` is ON (default OFF EVERYWHERE), each sweep runs the **lease-reaper first** (re-opens any expired-lease `running` job and marks its `apollo_ingest_runs` row `failed` so a crashed run is never left `running`, §9 OPS-5) then claims one `apollo_provisioning_jobs` row under SKIP-LOCKED (frozen 3B2f) and runs the 6-stage `run_provisioning` orchestrator over that document (scrape→find-or-generate→pair→tag/mint→promote), completing or failing the job on the run outcome — with cooperative SIGTERM/SIGINT shutdown between drains. It is **scaled to 0 replicas while dormant** so it costs no replica AND no auto-provisioned content can reach a student without an explicit human calibration/deploy step (§8B.3 / OPS-6). The flag-OFF default is the headline safety: the WU-3B2g enqueue (at the teacher-upload finalize seam, `domain-data.md`) ALWAYS writes the `pending` job + `queued` run, but with the flag OFF nothing DRAINS it, so a teacher upload is byte-identical to today. Activation is a HUMAN deploy step — flip `APOLLO_AUTOPROVISION_ENABLED` (after the §6.7 calibration gate) then scale to 1. See `apollo.md` for the worker/orchestrator/promote internals and `2026-06-20-apollo-kg-wu3b2g-orchestrator-plan.md` for the full design.

## Key dependencies

From `requirements.txt` (unpinned except floors): `fastapi` + `uvicorn[standard]` + `gunicorn` (serving), `pydantic>=2`, `SQLAlchemy[asyncio]>=2` + `asyncpg` + `pgvector` (Supabase Postgres), `openai` + `tiktoken` (LLM/embeddings), `pymupdf` (PDF extraction), `weasyprint`/`Markdown`/`pygments` (PDF report rendering — weasyprint needs native pango/cairo, see CI setup action), `neo4j>=5.27,<6` (Apollo KG), `sympy`, `numpy`, `aiosqlite`, `python-multipart` (optional at runtime — upload routes degrade to 503 without it), `requests`, `python-dotenv`. Test-only deps live in `requirements-test.txt`.

## Non-obvious conventions

- **Sync endpoints by design.** `/ask` is deliberately a sync `def` (FastAPI auto-threads it); a code comment forbids converting to `async def` unless the whole pipeline goes async. Sync code reaches async SQLAlchemy via `database.session.run_async()`, which runs coroutines on a shared background event loop so asyncpg connections stay alive across requests.
- **Auth is per-endpoint, not middleware.** Every protected route explicitly calls `_resolve_request_auth` / `_require_course_membership`. There is no auth middleware to hook.
- **`search_space_id` is the canonical course key.** The `class` field on `AskRequest` is deprecated/ignored; `doc_sets` overrides are hard-rejected.
- **Wire logs ride the response.** Pipeline stages `print()` lines prefixed `[Main AI`/`[Indexer AI`; `/ask` captures stdout and returns those lines in `logs`. Gated by `RETRIEVAL_WIRE_LOG`.
- **Error detail is opt-in.** 500 bodies are generic unless `DEBUG_HTTP_ERRORS=1`.
- **Root `supabase_client.py` is a shim** — import `vendors.supabase_client` in new code.
- **`python server.py` is stale**: the `__main__` block runs `uvicorn.run("backend.server:app", ...)`, a leftover module path. Use `uvicorn server:app` (what the Procfile does).
- **Auto-enroll**: first student access to a course silently creates a membership (`AUTO_ENROLL_STUDENT_MEMBERSHIP=1` default; restrict with `AUTO_ENROLL_SEARCH_SPACE_IDS`).
- **Pytest** (`pytest.ini`): `asyncio_mode = auto`, `--strict-markers`, markers `unit` / `integration` / `e2e` / `slow` / `llm`.
- **Coverage** (`.coveragerc`): `concurrency = thread, greenlet` is load-bearing — SQLAlchemy's asyncio bridge runs DB work through greenlets, and without greenlet tracing coverage silently drops every line after the first `await db.execute(...)` in a coroutine (which starved the diff-cover patch gate). `thread` must stay listed alongside it or TestClient's portal thread goes untraced.

### CI shape (one paragraph)
`.github/workflows/ci.yml` runs on PRs/pushes to `main`, `staging`, `ApolloV3` with five jobs: `quality` (ruff on changed files only — blocking for *added* files, advisory for *modified*, because the legacy tree has ~360 ruff errors), `typecheck` (mypy, advisory until Phase 3), `unit` (fast `-m "not integration"`, no Docker), `integration` (full suite on pgvector + Neo4j Testcontainers, plus a diff-cover ≥80% patch-coverage gate that is skipped on promotion PRs into ApolloV3), and `ci-passed` — the single required branch-protection status that asserts quality+unit+integration all passed. `nightly.yml` runs the full suite (incl. e2e/slow) on a 3.11/3.12 matrix with a ratcheted project coverage floor (currently 20%) and an advisory `pip-audit`. Both reuse the composite `.github/actions/setup` action (SHA-pinned actions, pip cache, weasyprint native libs). `.pre-commit-config.yaml` mirrors the CI ruff config (ruff + ruff-format on staged files) plus hygiene hooks (trailing whitespace, large files, private-key detection). `dependabot.yml` is also present.

## Product context

Hoot serves two roles per course ("search space"): **teachers** upload weekly notes/slides, tune retrieval weights per material kind, and manage invite links; **students** join via invite codes (or auto-enroll) and ask questions in chat sessions whose answers are grounded in course materials with mandatory citations. The web process answers questions; the worker process ingests teacher uploads asynchronously so uploads never block the UI. Deployment target is Railway (ApolloV3 branch), Procfile-driven.
