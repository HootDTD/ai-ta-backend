# Testing & CI — Aggregate Handoff (Phases 0–1 done → Phase 2 next)

> **Repo:** `HootDTD/ai-ta-backend`. **Updated:** 2026-06-09.
> **Ambition:** "balanced" — full test pyramid + regression, ~65–70% coverage gate on new code.
> **This file is the single source of truth.** It supersedes the Phase-0-era handoff and
> aggregates everything a cold session needs to continue at **Phase 2 (CI/CD wiring)**.
> **Companion:** `docs/TESTING-CI-PLAN.md` (full phased plan + reference configs),
> `.feller/tasks/2026-06-09-backend-testing-ci/RESEARCH-ARCHIVE.md` (citations).

---

## 1. TL;DR — where we are

- **Phase 0 (stabilize suite + honest CI gate): DONE & merged to `staging`.**
- **Phase 1 (real Postgres+pgvector + Neo4j test harness): DONE & MERGED** to `staging`
  (PR #3, merge commit `2bf0232`).
- **Phase 2 (full CI/CD wiring): DONE.** Code merged via **PR #4** (merge `7569cd0` to
  `staging`); `ci-passed` green incl. the first containerized `integration` run. Admin done
  2026-06-09: repo made **public**, **default branch → `staging`** (was stale `main` — nightly
  cron + Dependabot only run from the default branch and were dead before), **rulesets active**
  on `staging`+`ApolloV3` (require PR, required check `ci-passed`, block force-push/deletion;
  0 approvals — solo; NO linear-history — promotion uses merge commits). ci.yml's docs
  `paths-ignore` was REMOVED: a required `ci-passed` check must report on every PR or
  docs-only PRs deadlock (and a same-named no-op workflow races on mixed PRs).
  **Deploy platform: Railway (pending hookup)** — Heroku is dead/abandoned (last deploy
  2026-04-07; `ApolloV3` pushes since then never deployed) → see **`docs/PHASE2-ADMIN-SETUP.md`** §3.
- **Suite today:** `pytest -q` → **132 passed / 4 skipped / 0 failed** (real containers).
  PR-gate (`-m "not integration"`) → **121 passed / 4 skipped / 11 deselected**.

### Branch & PR state
| Branch | Role | Notes |
|---|---|---|
| `ApolloV3` | **Production line** (keep this name; Railway will deploy from it — Heroku abandoned) | downstream of staging |
| `staging` | Integration/QA | Phase 0 + Phase 1 (PR #3) merged |
| `test/phase2-cicd` | **Phase 2 work** (CI/CD wiring) | off `staging`; PR open |
| `test/phase1-harness` | Phase 1 (PR #3 — **merged**) | can be deleted |
| `main` | stale legacy (105+ behind ApolloV3) | not the trunk |

**Promotion flow:** `feature/* → staging → ApolloV3 (prod)`. Never merge unreviewed → ApolloV3.

**Commits on `test/phase1-harness` (ahead of staging):**
- `059157c` test: Phase 1 — real pgvector harness (Testcontainers + AsyncClient)
- `1546bac` test(apollo): diff-at-Done test shapes — *user's apollo WIP, committed onto this
  branch concurrently; rides along in PR #3 per author's choice* (see §9 git-race note)
- `01da9c8` test: Phase 1 follow-ups — Neo4j harness, VCR replay, factories, conftest de-dup
- `5cef791` ci: install requirements-test.txt; split PR (fast) vs push (full)

---

## 2. How to run the suite (from `ai-ta-backend/`)

```bash
# Windows venv (this machine). Docker Desktop must be running for integration tests.
.venv/Scripts/python.exe -m pytest -q                       # full suite (132 pass / 4 skip)
.venv/Scripts/python.exe -m pytest -m "not integration" -q  # fast PR subset (no Docker)
.venv/Scripts/python.exe -m pytest -m unit -q               # strict unit-marked only
.venv/Scripts/python.exe -m pytest --cov --cov-report=term-missing
.venv/Scripts/python.exe -m ruff check .                    # lint
# docker.exe lives at: C:\Program Files\Docker\Docker\resources\bin (add to bash PATH)
```

- **Use `.venv` Python.** A no-venv run uses system Python (no fastapi/pydantic) and falsely
  reports import "errors" — that earlier "49 passed / 62 errors" audit was this artifact.
- **Install test deps:** `pip install -r requirements-test.txt` (test-only; never in
  `requirements.txt` / never shipped to prod).
- **Docker is installed** (Docker Desktop, daemon 29.5.3; WSL2 Ubuntu present). Integration
  tests spin real containers; they **skip cleanly** if the daemon is down.

The 4 skips are intentional: `test_workspaces` (obsolete code removed), `test_knowledge_stores`
(over-mock quarantined → Phase 4 rewrite), `test_full_pipeline` + `test_retrieval` (manual smoke).

---

## 3. The Phase 1 test harness — how it works

All fixtures live in `tests/conftest.py`. They are **opt-in**: a test only triggers a container
by requesting `db_session` / `neo4j_client`, so unit runs never start Docker.

### pgvector (`db_session`)
- `_pg_url` (**session-scoped**): starts `pgvector/pgvector:pg16` Testcontainer, runs
  `CREATE EXTENSION vector` + `Base.metadata.create_all` once. `pytest.skip` if Docker is down.
- `db_session` (**function-scoped**): per-test `AsyncSession` with **transactional rollback**
  (`join_transaction_mode="create_savepoint"`). Uses a **per-test `NullPool` engine** so the
  connection is created on the test's own event loop — this avoids the cross-loop asyncpg pool
  bug documented in `database/session.py`. Every test starts with a pristine DB.

### Async API client
- `tests/support.override_db_session(app, session)` points an app's `get_db_session` dependency
  at the test session. Drive endpoints with `httpx.AsyncClient` + `ASGITransport` — **never**
  starlette `TestClient` (it breaks on asyncpg's event loop). Clear overrides in teardown.

### Neo4j (`neo4j_client`)
- `_neo4j_conn` (**session-scoped**): `neo4j:5.25` Testcontainer, Docker-guarded skip.
- `neo4j_client` (**function-scoped**): apollo `Neo4jClient` on a **wiped graph**
  (`MATCH (n) DETACH DELETE n` on setup + teardown). For apollo KG integration.

### Deterministic fakes (`tests/fakes/`)
- `fake_embedding(text)` → seeded **unit-norm** vector, dim **3072** (matches `EMBEDDING_DIM`).
- `one_hot_embedding(i)` → axis-aligned vector for hand-computable distances.
- `FakeOpenAI` → chat returns canned JSON; embeddings via `fake_embedding`.

### Factories (`tests/factories/`)
- factory_boy **builders** (`.build()` = unsaved) + async `persist(session, obj)` helper
  (factory_boy's SQLAlchemy persistence is sync-only — keep persistence explicit & loop-safe).
- Covers: SearchSpace, AITADocument, AITAChunk, CourseMembership, ChatSession, TeacherUpload.

### LLM replay (`tests/support/vcr.py`)
- `use_cassette(name)` / `build_vcr()`: secret-scrubbed cassettes (auth/api-key headers),
  body-sensitive matching, `record_mode` forced to **`none` in CI** (a missing cassette fails
  loudly instead of calling the live API). Cassettes in `tests/cassettes/`.
- Record locally with `VCR_RECORD_MODE=once`.

### Shared Supabase mock (`tests/support/supabase_mock.py`)
- One PostgREST-style in-memory mock used by **both** `tests/conftest.py` (root) and
  `tests/functions-tests/conftest.py`. `auto_id` flag preserves each suite's historical insert
  behaviour. (De-duped ~200 lines.)

### Test layout & markers
- `tests/{unit,integration}/` (+ legacy `tests/router/`, `tests/database/`, `tests/functions-tests/`).
- Markers (in `pytest.ini`, `--strict-markers`): `unit`, `integration`, `e2e`, `slow`, `llm`.
  `asyncio_mode=auto`.
- **Integration tests** (real containers) are marked `@pytest.mark.integration`. Everything else
  (unit-marked + unmarked) is selected by `-m "not integration"`.

### Key files touched in Phase 1
```
tests/conftest.py                 # db_session + neo4j fixtures, shared-mock wiring
tests/functions-tests/conftest.py # uses shared SupabaseMock(auto_id=True)
tests/fakes/{__init__,embeddings,openai_client}.py
tests/factories/{__init__,models}.py
tests/support/{__init__,vcr,supabase_mock}.py
tests/unit/test_vcr_config.py
tests/integration/test_pgvector_harness.py    # exit criterion #1 (distance ordering)
tests/integration/test_async_client_smoke.py  # exit criterion #2 (AsyncClient + rollback)
tests/integration/test_neo4j_harness.py
.coveragerc                       # branch + relative_files (gate is Phase 2)
requirements-test.txt             # user-approved test-only deps
database/models.py                # incidental fix: TeacherCourse.weight_bounds default
.github/workflows/main.yml        # install test deps + PR/push split
```

### Gotchas already solved (don't reintroduce)
1. **Do NOT call `pgvector.asyncpg.register_vector`** with the SQLAlchemy `Vector` type — it
   double-encodes (the type already serializes to the pgvector text format) → asyncpg
   "could not convert string to float". `register_vector` is only for RAW asyncpg queries.
2. **`create_all` setup connection must not register the vector codec** — its first connection
   is the one running `CREATE EXTENSION`, so the `vector` type doesn't exist yet. DDL needs no codec.
3. **Model bug fixed:** `TeacherCourse.weight_bounds` `server_default` had unescaped JSON colons;
   SQLAlchemy `text()` parsed `:0`/`:1` as bind params → `create_all` emitted `{"min"NULL.0,...}`.
   Escaped as `\:`. (Prod uses raw SQL migrations, so prod was unaffected.)

---

## 4. CI today (`.github/workflows/main.yml`)

Single `test` job on `ubuntu-latest`, triggers on push + PR to `[main, staging, ApolloV3]`.
**No `|| true`** — a red suite blocks the PR.

- Installs system libs for weasyprint (pango/cairo), then `requirements.txt` **and**
  `requirements-test.txt`.
- **Test selection (handoff split):**
  - `pull_request` → `pytest -m "not integration"` (unit/fast, **no containers**) — the green gate.
  - `push` / `workflow_dispatch` → `pytest -q` (**full suite**; Testcontainers spin pgvector +
    neo4j on the Docker-enabled runner).
- `CI=true` forces VCR replay-only.
- `concurrency` cancels superseded PR runs only.

**Do we run unit tests?** Yes — on both PR and push (PR runs unit + all non-integration).
**Do we run pgTAP?** **No.** This project has no pgTAP / `pg_prove` / `*.sql` tests. The DB layer
is tested in **Python** (pytest + SQLAlchemy against a real pgvector container). Adding a pgTAP
lane for RLS/functions is an **open decision** for Phase 2/3 (see §7).

---

## 5. Critical flows to cover (architecture map, for Phases 3–5)

- **A. Teacher upload → index:** `POST /teacher/upload` → `teacher_upload_worker` →
  `indexing/document_chunker` → OCR (`ocr/factory`) → `indexing/document_embedder` →
  `AITADocument` (pgvector). Mock: OpenAI embeddings, OCR, file I/O.
- **B. Student ask → answer:** `POST /ask` (+`/ask/stream`) → auth/workspace → parse → keywords →
  relevance gate → `retrieval/pipeline` (pgvector + FTS + RRF + rerank + pack) →
  `ai/main_ai.solve_with_bundle` → `citations/formatter` → persist. Mock: OpenAI, auth; real: pgvector.
- **C. Apollo session:** `POST /apollo/sessions/from_hoot` / `/apollo/session/{id}/chat` →
  `apollo/handlers` → `apollo/parser` (LLM) → `apollo/solver` → `apollo/persistence` (Neo4j).
  Mock: OpenAI; real/container: Neo4j.

Good mocking seams: `auth.py`, `vendors/supabase_client.py`, `database/session.py`,
apollo Neo4j singleton, `indexing/document_embedder`.

---

## 6. Constraints & decisions — DO NOT violate

- **pgvector ≠ SQLite.** Never "fix" a DB test by leaning on the SQLite mock; use real Postgres
  (`pgvector/pgvector` image). It hides type / distance-operator / concurrency bugs (Phase 1
  literally caught the `weight_bounds` bug this way).
- **Never call live LLM/embedding APIs in CI.** Mock (unit) / replay via VCR (integration). Real
  model eval is nightly only. Sub-second timeouts on LLM-path tests (`pytest-timeout`).
- **`AsyncClient` + `ASGITransport`, not `TestClient`.**
- **No new packages without explicit user confirmation** (CLAUDE.md). Test deps go in
  `requirements-test.txt`.
- **Never push directly to `main`/`ApolloV3`; always feature branch → PR.**
- **Keep `ApolloV3` as the prod branch name** (deploy platform is **Railway** — Heroku is
  abandoned; confirm the Railway-connected branch before any branch surgery).
- **Citations are non-negotiable** — never remove/bypass citation markers or the semantic
  relevance gate.
- **Coverage gate:** enforce **patch coverage (~80% on new code)** + ratchet the project floor
  from baseline (~22% measured). Do NOT impose a flat repo-wide floor on the current codebase.

---

## 7. Phase 2 — Full CI/CD wiring  *(CODE DONE; admin pending)*

**Goal:** turn the single gate into a proper parallel pipeline with coverage gating, branch
protection, and deploy gates. Full reference YAML is in `docs/TESTING-CI-PLAN.md` §3 + §6.

### What was built (on `test/phase2-cicd`)
- `.github/workflows/ci.yml` — parallel `quality` (ruff, changed-files ratchet: added=blocking,
  modified=advisory) · `typecheck` (mypy, advisory/non-blocking) · `unit` (`-m "not integration"`,
  no Docker) · `integration` (FULL suite on pgvector+Neo4j **Testcontainers**, single-process) +
  **`ci-passed`** aggregation (the one required check). Patch-coverage gate = `diff-cover ≥80%`
  vs the PR base. Triggers PR+push to `[main, staging, ApolloV3]`, `paths-ignore` docs.
- `.github/actions/setup/action.yml` — composite (setup-python+pip-cache+weasyprint libs+install),
  reused by all jobs. **All actions SHA-pinned** (checkout 4.2.2, setup-python 5.3.0, upload-artifact 4.6.0).
- `.github/workflows/nightly.yml` — cron 06:00 UTC: full+e2e on 3.11/3.12 matrix, **project floor**
  `coverage --fail-under=20` (ratchet up from ~22% baseline), advisory `pip-audit`. RAG eval = Phase 5.
- `ruff.toml` (select E/F/I/B/UP, py311, len 100) · `mypy.ini` (lenient, advisory) ·
  `.pre-commit-config.yaml` (ruff+ruff-format+hygiene) · `.github/dependabot.yml` (actions+pip weekly) ·
  `.gitattributes` (force LF on yml/sh/py — kills the §9 CRLF risk). `main.yml` deleted (superseded).

### Decisions locked this phase
- Integration DB in CI = **Testcontainers** (reuse Phase 1 harness; zero fixture rewrite).
- **No `uv`** (kept pip-cache; no lockfile exists) · **pgTAP deferred** (no pgTAP tests; RLS = Phase 3/4).
- Ruff/mypy enforced **changed-files only** (legacy tree has 360 ruff errors / 1689 format diffs;
  repo-wide gate would red-fail every PR). New code strict; legacy on a ratchet.
- `ApolloV3` **kept** as prod branch (no rename — Heroku risk).

### Remaining = admin → `docs/PHASE2-ADMIN-SETUP.md` *(DONE 2026-06-09 except Railway hookup)*
Branch-protection rulesets on `staging`+`ApolloV3` (required check `ci-passed`), Environments +
prod-scoped secrets (no Env reviewer — auto-approve), Heroku "wait for CI", dry-run promotion.
**Order:** let `ci.yml` go green on the Phase 2 PR **once**, *then* enable branch protection.

### Build (reference — original task list)
1. **Composite `setup` action** (`.github/actions/setup`): Python + `uv` (or pip cache) + system
   libs, reused by all jobs. SHA-pin all third-party actions.
2. **`ci.yml` — parallel jobs** (replaces today's single `test` job):
   - `quality` — `ruff check` + `ruff format --check` + `mypy`.
   - `unit` — `pytest -m "unit" -q` (or `-m "not integration"`), fast, no services.
   - `integration` — `pytest -m integration -q` with **either** a `services: pgvector/pgvector:pg16`
     service container **or** keep Testcontainers (decision below). Neo4j service/container for apollo.
   - `ci-passed` — aggregation gate that depends on all three (this is the required check for
     branch protection).
3. **Coverage gate:** `pytest --cov` → `coverage xml` → `diff-cover --compare-branch=origin/staging
   --fail-under=80`. Ratchet a project-floor check separately. (`.coveragerc` already set:
   `branch=true`, `relative_files=true`.)
4. **`nightly.yml`** (cron, off the PR path): full + e2e + RAG eval (DeepEval/RAGAS thresholds) +
   `pip-audit`.
5. **Branch protection rulesets** on `staging` + `ApolloV3`: require `ci-passed`, require PR,
   linear history. ⚠️ Private-repo *required reviewers* on Environments need GitHub Enterprise —
   user wants **prod deploy AUTO-APPROVE**, so use a non-blocking deploy or branch-policy-only gate.
6. **GitHub Environments** (`staging` / `production`) with prod secrets scoped to the prod env.
7. **Dependabot** + **pre-commit** (ruff/mypy/secrets).

### Decisions to make at the start of Phase 2
- **Integration DB in CI: Testcontainers vs `services:` container.** Phase 1 uses Testcontainers
  (works locally + on GH runners). The plan's §3 suggested a `services: pgvector/pgvector:pg16`
  container (faster startup, no Docker-in-Docker concerns). Pick one for the `integration` job and
  set `TEST_DATABASE_URL` accordingly (the harness already supports a daemon-driven container; a
  `services` container would need the fixtures to honor an injected URL — small adaptation).
- **pgTAP lane?** Optional. pgTAP is strong for **RLS policies + SQL functions** (Phase 3/4 has RLS
  work). Current plan tests the DB layer in Python. Decide: add `pg_prove` job vs keep Python-only.
- **Strict unit lane.** Today the PR runs `-m "not integration"` (broad). Phase 2's `unit` job can
  tighten to `-m unit` once more tests are marked.

### Phase 2 exit
- PR shows `quality`, `unit`, `integration`, `ci-passed` checks; `ci-passed` is the required gate.
- Patch-coverage gate active (≥80% on new code) and visibly blocking under-covered PRs.
- Branch protection live on `staging` + `ApolloV3`. Nightly job runs.

---

## 8. Later phases (brief — full detail in `docs/TESTING-CI-PLAN.md` §4)

- **Phase 3 — Unit layer:** citations, config/contracts, `ai/main_ai` failure modes, `ai/router/*`,
  retrieval RRF + reranker + store_bias + context_packer, `indexing/document_chunker` (hypothesis),
  apollo `parser`/`solver`/`overseer`, `ai/vision`, `auth.py`.
- **Phase 4 — Integration (real DB + mocked LLM):** revive ~56 `test_e2e_api` cases as `AsyncClient`
  tests; `retrieval/pipeline` + `hybrid_search` on real pgvector; indexing round-trip; chats memory;
  auth enforcement; **rewrite quarantined `test_knowledge_stores`**; revive `test_migration_015`
  fully; apollo handlers on Neo4j container.
- **Phase 5 — E2E + regression + eval:** 3–6 critical journeys; `tests/regression/` + bug-repro
  convention; golden/snapshot only for deterministic outputs (never raw LLM text); nightly RAG eval.
- **Phase 6 — Ratchet & harden:** raise coverage floor; flaky-quarantine policy; keep PR CI < 5 min;
  document flow in `CLAUDE.md`/`docs/DATA-FLOW.md`.

---

## 9. Open risks / gotchas

- **First full (push/merge) CI run** starts the pgvector + neo4j Testcontainers for the first time
  in CI — watch for image-pull timeouts / container flakiness; tune `pytest-timeout` and container
  wait if needed. The PR gate (no containers) is already green.
- **Cross-platform:** harness authored on Windows/py3.12; CI runs Linux/py3.11. PR gate passed
  there (121), so the unit/unmarked tells are clean. Watch LF↔CRLF on Windows-authored files.
- **Concurrent-git race (resolved, noted):** the user committed apollo WIP onto `test/phase1-harness`
  while Phase 1 was being committed; commit `059157c` absorbed one apollo file deletion
  (`apollo/tests/test_e2e_misconception_smoke.py` — a legit deletion). Net tree correct. If working
  this repo in parallel again, use separate worktrees to avoid index races.
- **`run_async()` "coroutine was never awaited"** warnings in invite-link endpoints
  (`server.py:1166/1179/1223/1303`) during `test_e2e_api` — passing but a latent smell; investigate
  in Phase 3/4.
- **Pydantic v2 deprecations** (`class Config`, `on_event`) in `server.py` — tech debt, non-blocking.
- ~~Branch protection NOT set yet~~ — **rulesets active since 2026-06-09** (`protect-staging`,
  `protect-apollov3`; required check `ci-passed`).
- **Railway not yet connected** — prod (`ApolloV3`) currently deploys NOWHERE. Old Heroku app
  `backend-main` should be deleted/disconnected if it still exists.

---

## 10. References & environment
- `docs/TESTING-CI-PLAN.md` — full phased plan, gap matrix, reference CI/CD YAML.
- `.feller/tasks/2026-06-09-backend-testing-ci/RESEARCH-ARCHIVE.md` — cited best-practices research.
- **GitHub MCP** is connected (PRs/branch ops via MCP). **`gh` CLI is NOT installed.** Read Actions
  check-runs via `GET /repos/HootDTD/ai-ta-backend/commits/<sha>/check-runs` (the
  combined-*status* endpoint shows none — Actions report as *check runs*).
- **Infra:** Supabase project `uduxdniieeqbljtwocxy` (prod-ish; do NOT point tests at it — use
  ephemeral containers); Neo4j Aura for apollo prod; `EMBEDDING_DIM=3072` (text-embedding-3-large).
- **PR #3:** https://github.com/HootDTD/ai-ta-backend/pull/3 (base `staging`, green).
