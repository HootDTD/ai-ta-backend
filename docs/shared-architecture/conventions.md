---
doc: shared/conventions
description: Cross-repo coding, testing, CI, and branching conventions for the Hoot AI-TA backend and both Next.js UIs
owns: []
related:
  - ai-ta-backend/_overview
  - shared/security
last_verified: 2026-07-12
stub: false
---

# Conventions

Shared engineering conventions across the three Hoot AI-TA repos: `ai-ta-backend` (Python/FastAPI), `ai-ta-student-ui` and `ai-ta-teacher-ui` (Next.js). Grounded in `ai-ta-backend/CLAUDE.md`, the lint/type/test configs, and `ai-ta-backend/.github/workflows/ci.yml`.

## Python style and tooling (ai-ta-backend)

- **Ruff** is the single lint + format tool (`ai-ta-backend/ruff.toml`): target py311, line length 100, rule set `E, F, I, B, UP`, `E501` ignored (formatter owns line length), double quotes, space indent. FastAPI DI calls (`Depends`, `Query`, etc.) are whitelisted for B008; `tests/**` ignores B011; `__init__.py` ignores F401 (re-exports).
- **Ratchet philosophy**: the legacy tree carries ~360 lint findings / ~1689 format diffs, so ruff is enforced on **changed files only** — new code must be pristine, legacy is cleaned opportunistically, never in a big-bang reformat. Run `ruff check --fix` + `ruff format` locally before committing.
- **Mypy** is deliberately lenient (`ai-ta-backend/mypy.ini`): advisory, `ignore_missing_imports = True`, untyped defs allowed, `no_implicit_optional = True`. It excludes `database/migrations/` and `tests/`. Phase 3 of the testing plan tightens it.
- **Pre-commit** (`ai-ta-backend/.pre-commit-config.yaml`): ruff + ruff-format on staged files (mirrors CI's changed-files gate), plus trailing-whitespace, end-of-file-fixer, check-yaml, check-merge-conflict, large-file check (1 MB), and `detect-private-key` as a secret tripwire. Install with `pip install pre-commit && pre-commit install`.
- Excluded from any full-repo lint run: `.venv`, `database/migrations`, `__pycache__`.

## Testing strategy (ai-ta-backend)

Defined in `ai-ta-backend/pytest.ini`, `ai-ta-backend/tests/conftest.py`, and `ai-ta-backend/docs/TESTING-CI-PLAN.md` (Testing Trophy: integration-heavy, not the classic unit pyramid).

- `asyncio_mode = auto` — every `async def test_*` runs as a coroutine test, no decorator needed.
- `--strict-markers` — unregistered markers are errors. Registered markers: `unit` (pure logic, no DB/network), `integration` (real DB / external wiring, LLM mocked), `e2e` (full critical-path flow), `slow` (>1s), `llm` (exercises an LLM/embedding path, mocked or replayed).
- **Supabase mock**: an autouse `_mock_supabase` fixture routes `vendors.supabase_client` through the shared in-memory PostgREST-style mock in `ai-ta-backend/tests/support/supabase_mock.py`, and stubs the `openai` module with deterministic fakes. All new features must include unit tests using these fixtures (`ai-ta-backend/CLAUDE.md` coding standard).
- **Real pgvector**: SQLite cannot evaluate `<=>`/`<->` or HNSW, so retrieval/DB tests use a session-scoped `pgvector/pgvector:pg16` Testcontainer (`_pg_url`) and a function-scoped `db_session` fixture that rolls back a savepoint-joined transaction after each test. Skips cleanly if Docker is down.
- **Neo4j**: Apollo KG integration tests use a session-scoped `neo4j:5.25` container with a function-scoped `neo4j_client` fixture that wipes the graph before and after each test.
- LLM calls are never live in CI: `CI=true` forces VCR `record_mode=none`; fake embeddings are deterministic.
- Coverage expectation: **patch coverage ≥80% on new code** (diff-cover gate in CI); repo-wide coverage ratchets up over time per the plan (~65–70% target gate).
- Run locally: `pytest tests/ -v --tb=short`, or `pytest -m "not integration"` for the fast no-Docker subset.

## CI gates (ai-ta-backend/.github/workflows/ci.yml)

Triggered on PRs and pushes to `main`, `staging`, and the retired `ApolloV3`. Parallel jobs:

| Job | Gate |
|---|---|
| `quality` | Ruff on changed files — **blocking** for added files (check + format), advisory for modified files |
| `typecheck` | Mypy on changed files — advisory (`continue-on-error`), tightened in Phase 3 |
| `unit` | `pytest -m "not integration" -n auto` with coverage — blocking, no Docker |
| `integration` | Full suite on real pgvector + Neo4j Testcontainers, then diff-cover **≥80% patch coverage** — blocking. The skip condition still names the retired `ApolloV3` branch, so staging→`main` promotion PRs currently run the full patch gate (known drift in `ci.yml`; the ratchet was already enforced on the way into staging) |
| `ci-passed` | Aggregation job — the single required branch-protection status |

A nightly workflow (`ai-ta-backend/.github/workflows/nightly.yml`) exists for off-PR-path runs; evals and other nondeterministic/costly checks belong there, never on the PR path.

## TypeScript / Next.js conventions (both UIs)

- Next.js 15 (App Router) + React 19 + Tailwind CSS 4 + TypeScript 5, ESLint 9 flat config extending `next/core-web-vitals` + `next/typescript` (`ai-ta-student-ui/eslint.config.mjs`, identical in teacher UI).
- `tsconfig.json`: `strict: true`, `@/*` path alias to repo root.
- Scripts: `npm run dev` (Turbopack; student UI on port **3001**, teacher UI on port **3002**), `npm run build`, `npm run lint`. No test runner is configured in either UI yet.
- Backend access is always through **Next.js API route proxies** (`app/api/*/route.ts`) targeting `AI_TA_API_BASE_URL` — the browser never calls FastAPI directly. Proxies forward the `Authorization: Bearer` header and set `Cache-Control: no-store`.
- Component naming: PascalCase, feature-prefixed (e.g. `ApolloChat`, `ApolloKGPanel`, `ApolloPageClient` in `ai-ta-student-ui/components/apollo/`); server `page.tsx` wraps a `"use client"` page-client component in `Suspense`.
- Secrets: only `NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_ANON_KEY` are client-visible; everything else stays server-side in the proxy layer.

## Branch and deploy conventions

- **Never push directly to `main`** — always work on a feature branch and open a PR (`ai-ta-backend/CLAUDE.md`).
- Branch model (pilot era, 2026-07 — authority: `ai-ta-backend/docs/branching.md`): `staging` is the trunk (feature branches → PR → staging with the full CI gate); **`main` is the pilot release/prod branch**, moved only by staging→main promotion PRs and `hotfix/*` PRs (auto back-merged into staging). `ApolloV3` is the RETIRED former prod branch — stale since 2026-06-09; do not target it.
- **Deployment is Railway** via GitHub integration: the **production** environment deploys `main` (backend, student-ui, teacher-ui; the prod backend-worker service alone is still pinned to legacy `ApolloV3`, deliberately — decision 2026-07-12), and the **staging** environment deploys `staging`. `ai-ta-backend/Procfile` defines the web + worker processes. Heroku is abandoned — do not re-wire it.
- Commit messages: conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, ...).

## Naming patterns observed in code

- Python: snake_case modules organized by domain (`retrieval/`, `indexing/`, `knowledge/`, `chats/`, `apollo/`, `citations/`); handler functions `handle_*` (e.g. `apollo/handlers/done.py:handle_done`); domain-specific exception classes per module (`apollo/errors.py`: `FilterRejectedError`, `SessionFrozenError`, ...).
- Database: legacy tables carry the `aita_` prefix (`aita_search_spaces`, `aita_documents`, `aita_chunks`) with matching `AITA*` SQLAlchemy models; newer tables are plain snake_case (`chat_sessions`, `chat_turns`, `teacher_uploads`, `teacher_upload_jobs`, `course_memberships`, `course_invite_links`).
- Config: all runtime configuration via env vars surfaced through `config/settings.py` (e.g. `MAIN_MODEL`, `EMBEDDING_DIM`, `CHAT_MEMORY_WINDOW_TURNS`). Copy `.env.example` to `.env`; never modify `.env` files or commit secrets.

## Non-negotiable product rules (from ai-ta-backend/CLAUDE.md)

These are conventions in the strongest sense — violating them breaks the product contract:

1. **Structured JSON from the LLM layer, never raw text.** Every LLM call returns a parsed, schema-shaped response (e.g. `ParsedTask`, relevance verdicts, Apollo parser nodes/edges).
2. **Citation markers are non-negotiable.** Never remove or bypass citation marker generation (`[S1]`-style markers assigned in context packing, rendered as `[Label, p. N]` chips in the UIs). Every factual claim in an answer must carry one.
3. **The semantic filter is never bypassed.** Scope enforcement (relevance check rejecting out-of-scope questions) is a core product requirement, not an optimization.
4. **Do not change hybrid search fusion logic** without running the full retrieval test suite first.
5. **Each retrieval pipeline stage stays independent and independently testable**, with comprehensive debug logging.
6. **No new vector stores** (pgvector + FAISS only) and **no new packages** without explicit confirmation.
