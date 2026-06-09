# Testing & CI — Session Handoff

> **Created:** 2026-06-09. **Repo:** `ai-ta-backend` (GitHub `HootDTD/ai-ta-backend`).
> **Goal:** stand up a backend testing system + `staging` branch + gating CI, at "balanced"
> ambition (~65–70% coverage gate, full pyramid + regression).
> **Companion docs:** `docs/TESTING-CI-PLAN.md` (full phased plan + research) and
> `.feller/tasks/2026-06-09-backend-testing-ci/RESEARCH-ARCHIVE.md` (citations).
> Pick this up cold and you have everything to continue.

---

## 1. TL;DR — where we are

- **Phase 0 (stabilize the existing suite + honest CI) is DONE** and lives on branch
  `test/phase0-stabilization` (commit `4d3cc0d`, pushed). Verified: **117 passed / 7
  skipped / 0 failed / 0 errors**.
- **`staging` branch exists** (local + remote, from `ApolloV3` HEAD). `ApolloV3` remains
  the production line (a brief rename to `production` was tried and **reverted** — keep
  `ApolloV3`).
- **Phases 1–6 are NOT started.** Phase 1 (real Postgres+pgvector test harness) is the
  core remaining work — it's what makes the retrieval/indexing code actually testable.

### ⚠️ Reconciliation needed FIRST (state mismatch on `staging`)
The commit `296c9a2 "phase 0"` currently on `staging` is **mislabeled** — it contains the
**apollo WIP** (`apollo/api.py`, `handlers/chat.py`, `agent/output_filter.py`, + tests)
**plus a stray `uvicorn.log`**, NOT the test-stabilization work. So `staging` is missing
all the real Phase 0 changes and has a log file it shouldn't.

**Do this before Phase 1:**
1. Merge `test/phase0-stabilization` → `staging` (PR base `staging`, head
   `test/phase0-stabilization`). Brings the real Phase 0 + `docs/TESTING-CI-PLAN.md`.
   - There will be a small overlap in `apollo/*` files (the feature branch was cut before
     the WIP was committed). Resolve by keeping `staging`'s apollo versions — the Phase 0
     branch only touched apollo files incidentally via the branch point, not on purpose.
   - Safer alternative: cherry-pick only the non-apollo Phase 0 files onto `staging`
     (`pytest.ini`, `tests/conftest.py`, `tests/functions-tests/test_tutor_prompt.py`,
     `tests/functions-tests/test_knowledge_stores.py`, `tests/test_workspaces.py`,
     `.github/workflows/main.yml`, `docs/TESTING-CI-PLAN.md`, `docs/TESTING-CI-HANDOFF.md`).
2. Remove the log from version control: `git rm --cached uvicorn.log` and add to
   `.gitignore` (see §7).
3. Confirm green on `staging`: `pytest` → expect `117 passed / 7 skipped`.

---

## 2. Branch map

| Branch | Local | Remote | Role |
|---|---|---|---|
| `ApolloV3` | ✓ | ✓ | **Production line** (keep this name; do not rename). Heroku deploy line — verify before any branch surgery. |
| `staging` | ✓ | ✓ `296c9a2` | Integration/QA branch. Currently holds apollo WIP mislabeled "phase 0" + stray uvicorn.log; needs Phase 0 merged in (§1). |
| `test/phase0-stabilization` | ✓ | ✓ `4d3cc0d` | The real Phase 0 commit. Merge → `staging`. |
| `main` | ✓ | ✓ | **Stale legacy** — 105 commits *behind* `ApolloV3`. Not the trunk. |
| `ApolloV2`, `ApolloV4`, … | — | ✓ | Older lines, ignore. |

**Intended flow:** `feature/* → staging → ApolloV3 (prod)`. Downstream-only; never merge
`staging → ApolloV3` with unreviewed work. Fast tests on PR-to-staging; full
integration/e2e on merge-to-staging; smoke on prod.

---

## 3. The true baseline (correct numbers)

An early audit wrongly reported "49 passed / 62 errors" — that was run with the **system
Python, no `.venv`**, so every `fastapi`/`pydantic` import looked like an error. **There is
no `server.py` import crisis.** Real numbers (always use the venv):

```
START (pre-Phase-0):  113 passed · 5 failed · 2 skipped · 4 errors  (1 collection error aborted the run)
AFTER Phase 0:        117 passed · 7 skipped · 0 failed · 0 errors
```

Run the suite (from `ai-ta-backend/`):
```bash
.venv/Scripts/python.exe -m pytest -q          # Windows
# or: python -m pytest -q   (if venv active)
```

The 7 skips are all explicit: 3 × `test_migration_015` (need real Postgres → Phase 1),
1 × `test_workspaces` (obsolete, code removed), 1 × `test_knowledge_stores` (quarantined
over-mock → Phase 4 rewrite), 2 × manual smoke (`test_full_pipeline`, `test_retrieval`).

---

## 4. What Phase 0 changed (reference)

On `test/phase0-stabilization`:
- **`pytest.ini`** — registered markers (`unit/integration/e2e/slow/llm`), `asyncio_mode=auto`
  (async router tests were silently skipped before), `--strict-markers`.
- **`tests/conftest.py`** — added a `db_session` fixture that **skips cleanly** when
  `TEST_DATABASE_URL` is unset (so the integration migration tests don't error). Phase 1
  replaces it with the real Testcontainers engine.
- **`tests/functions-tests/test_tutor_prompt.py`** — de-brittled 4 stale assertions to check
  rule *intent* against current prompt wording (one even contradicted its own comment).
- **`tests/functions-tests/test_knowledge_stores.py`** — `@pytest.mark.skip` the over-mocked
  test (it patched an exact `run_async` call sequence that drifted: `assert 0 >= 100`).
- **`tests/test_workspaces.py`** — module-level skip; it imported
  `build_local_static_workspace_config`, deleted in the pgvector/Supabase migration.
- **`.github/workflows/main.yml`** — replaced the decorative `|| true` CI with a real gate
  on `main`/`staging`/`ApolloV3` (note: update the trigger list — it currently says
  `production`; change to `ApolloV3` since the rename was reverted), `concurrency`
  cancel-on-PR, weasyprint apt deps, `pytest -q` with no `|| true`.

**⚠️ Action:** the CI workflow's branch triggers list `production` — since `ApolloV3` is kept,
edit `.github/workflows/main.yml` `on.push.branches` / `on.pull_request.branches` to
`[main, staging, ApolloV3]`.

---

## 5. Remaining phases (full detail in `docs/TESTING-CI-PLAN.md` §4)

### Phase 1 — Test harness & infrastructure  ← **NEXT, the core work**
The thing that unblocks everything: pgvector/HNSW and `<=>`/`<->` operators **do not exist
in SQLite**, so retrieval/indexing can't be tested today. Build:
1. **Real-Postgres fixture** — session-scoped Testcontainers `PostgresContainer("pgvector/pgvector:pg16")`;
   async engine (`NullPool`); `CREATE EXTENSION vector` + `Base.metadata.create_all` (or
   `alembic upgrade head` if migrations are Alembic — verify, the repo has numbered SQL + 015/021);
   register pgvector type on asyncpg connect.
2. **Transactional isolation** — function-scoped `db_session`: connection → outer txn →
   session bound to it → SAVEPOINT re-open on `after_transaction_end` → rollback in teardown.
   Replace the Phase 0 skip-stub. Keep a few non-transactional tests for HNSW recall.
3. **Async API client** — `AsyncClient(transport=ASGITransport(app=app))` fixture (NOT
   `TestClient` — it breaks on asyncpg's event loop); override `get_db_session`, auth, and
   the OpenAI/embedding dependency; `app.dependency_overrides.clear()` in teardown.
4. **Deterministic fakes** — hash-seeded `fake_embeddings(text) -> vector` (dim=`EMBEDDING_DIM`,
   3072); fake OpenAI chat client; promote the existing conftest stubs into `tests/fakes/`.
5. **LLM replay** — VCR.py (`record_mode="none"` in CI, filter `authorization`/`x-api-key`,
   match on body) for integration; programmable fake server for tool-call loops.
6. **Factories** — `tests/factories/` factory_boy builders for `SearchSpace`, `AITADocument`,
   `ChatSession`, `CourseMembership`, `TeacherUpload`, apollo models.
7. **Layout** — `tests/{unit,integration,e2e}/` with nested `conftest.py` so the DB
   container doesn't spin up for unit runs; de-dup the two overlapping conftest supabase mocks.
8. **Coverage wiring** — `[tool.coverage.run] relative_files=true, branch=true`; `diff-cover`
   vs `origin/staging`.
9. **Neo4j test container** — for apollo integration (prod uses Neo4j Aura).

**New test deps (⚠️ CLAUDE.md says confirm before installing packages):** `pytest-cov`,
`pytest-mock`, `pytest-xdist`, `pytest-timeout`, `pytest-randomly`, `respx`, `vcrpy`,
`freezegun`, `hypothesis`, `factory-boy`, `testcontainers[postgres]`, `diff-cover`, `ruff`,
`mypy`. Put them in a new `requirements-test.txt`. **Get explicit OK first.**

**Exit:** a sample integration test asserts pgvector distance ordering on known fake vectors,
and a sample endpoint test runs via `AsyncClient` with a rolled-back DB — green locally + in CI.

### Phase 2 — Full CI/CD wiring
Composite `setup` action (uv); parallel `ci.yml` jobs (quality/unit/integration with
`services: pgvector/pgvector:pg16` + `ci-passed` aggregation check); `nightly.yml`
(full+e2e+eval+pip-audit); patch-coverage gate ≥80% + ratcheted project floor; branch
protection rulesets on `ApolloV3`/`staging`; GitHub Environments (`staging`/`production`)
with prod secrets scoped to the prod env; **prod deploy gate set to AUTO-APPROVE per user**;
SHA-pin actions + Dependabot; pre-commit. ⚠️ Private-repo Environment *required reviewers*
need GitHub Enterprise — user wants auto-approve anyway, so use a non-blocking deploy or a
branch-policy-only gate. See PLAN §3 + §6 for the reference YAML.

### Phase 3 — Unit layer (pure logic)
citations, config/contracts, `ai/main_ai` (failure modes), `ai/router/*`, retrieval RRF math
+ reranker scoring + store_bias + context_packer, `indexing/document_chunker` (hypothesis),
`apollo/parser` + `solver/forward_chain` + `overseer/misconception_bank`, `ai/vision`,
`auth.py` parsing.

### Phase 4 — Integration layer (real DB + mocked LLM)
Revive the ~56 quarantine-worthy `test_e2e_api` cases as `AsyncClient` tests asserting
behavior; `retrieval/pipeline`+`hybrid_search` on real pgvector; indexing round-trip;
`knowledge/manager` CRUD; `chats/service` memory; `auth` enforcement; `database` models +
migration 015/021 on real Postgres (revive `test_migration_015` with the real `db_session`);
**rewrite the quarantined `test_knowledge_stores`**; apollo handlers on Testcontainers Neo4j.

### Phase 5 — E2E + regression + eval
3–6 critical journeys (teacher upload→index→searchable; student ask→retrieve→answer+citations;
apollo session lifecycle); `tests/regression/` + bug-repro convention; golden/snapshot ONLY
for deterministic outputs (never raw LLM text); nightly RAG eval (DeepEval/RAGAS thresholds)
off the PR path.

### Phase 6 — Ratchet & harden
Raise coverage floor; flaky quarantine policy; keep PR CI < 5 min; document flow in
`CLAUDE.md`/`docs/DATA-FLOW.md`; revisit deferred contract tests when there are paying users.

---

## 6. Critical flows to cover (from the architecture map)

- **A. Teacher upload → index:** `POST /teacher/upload` → `teacher_upload_worker` →
  `indexing/document_chunker` → OCR (`ocr/factory`) → `indexing/document_embedder` (OpenAI) →
  `AITADocument` (pgvector). Mock: OpenAI embeddings, OCR, file I/O.
- **B. Student ask → answer:** `POST /ask` (+`/ask/stream`) → auth/workspace → parse →
  keywords → relevance gate → `retrieval/pipeline` (pgvector + FTS + RRF + rerank + pack) →
  `ai/main_ai.solve_with_bundle` (GPT) → `citations/formatter` → persist turn. Mock: OpenAI,
  auth; real: pgvector.
- **C. Apollo session:** `POST /apollo/sessions/from_hoot` / `/apollo/session/{id}/chat` →
  `apollo/handlers` → `apollo/parser` (LLM) → `apollo/solver/forward_chain` →
  `apollo/persistence` (Neo4j). Mock: Neo4j (or container), OpenAI.

Mocking chokepoints (good seams): `auth.py`, `vendors/supabase_client.py`,
`database/session.py` (loop-aware engine), apollo Neo4j singleton, `indexing/document_embedder`.

---

## 7. Key constraints & decisions — DO NOT violate

- **pgvector ≠ SQLite.** Never "fix" DB tests by leaning on the SQLite mock; it hides type,
  distance-operator, and concurrency bugs. Real Postgres (`pgvector/pgvector` image) for the
  DB/retrieval layer. (Per `CLAUDE.md`: don't change hybrid-search fusion without running the
  full retrieval suite.)
- **Never call live LLM/embedding APIs in CI.** Mock (unit) / replay (integration). Real-model
  RAG eval is nightly only. Always set sub-second timeouts on LLM-path tests.
- **`AsyncClient` + `ASGITransport`, not `TestClient`** for the async app.
- **Use `.venv` Python** for all local test runs.
- **No new packages without explicit user confirmation** (`CLAUDE.md`). Phase 1 needs the deps
  in §5 — ask first; put in `requirements-test.txt`.
- **Never push directly to `main`/`ApolloV3`; always feature branch → PR** (`CLAUDE.md`).
- **Keep `ApolloV3` as the prod branch name.** Heroku deploys via Procfile — confirm the
  Heroku-connected branch before any branch surgery.
- **Citations are non-negotiable** — never remove/bypass citation-marker generation or the
  semantic relevance gate (`CLAUDE.md`).
- **Coverage gate:** enforce **patch coverage (~80% on new code)**, ratchet the project floor
  from baseline — do NOT impose a flat repo-wide floor on the current ~40–50% codebase.
- **`.gitignore` additions needed:** `uvicorn.log`, `*.log`, `.pytest_cache/`,
  `__pycache__/`, `.coverage`, `coverage.xml`, `.venv/` (verify which are already ignored).

---

## 8. Commands cheat sheet

```bash
# from ai-ta-backend/
.venv/Scripts/python.exe -m pytest -q                 # full suite (expect 117 pass / 7 skip post-merge)
.venv/Scripts/python.exe -m pytest -m unit -q          # unit only
.venv/Scripts/python.exe -m pytest -m "not integration" -q
.venv/Scripts/python.exe -m pytest tests/router -q     # a folder

# reconcile staging (see §1)
git checkout staging
git merge test/phase0-stabilization        # or cherry-pick the non-apollo files
git rm --cached uvicorn.log && echo "uvicorn.log" >> .gitignore

# open the Phase 0 PR (MCP now connected, or via link)
# base: staging  ←  head: test/phase0-stabilization
# https://github.com/HootDTD/ai-ta-backend/pull/new/test/phase0-stabilization
```

GitHub MCP is connected (reconnected 2026-06-09) — PRs/branch ops can go through it.
`gh` CLI is NOT installed locally.

---

## 9. Open risks / gotchas

- **First CI run will likely need a tweak** — `weasyprint` native libs (apt step added) and
  LF↔CRLF on Windows-authored files. Watch the first Actions run on the Phase 0 PR.
- **`run_async()` "coroutine was never awaited" warnings** in the invite-link endpoints
  (`server.py:1166/1179/1223/1303`) during `test_e2e_api` — tests pass but this is a latent
  smell; investigate whether it's a test-mock artifact or a real bug in those endpoints.
- **Pydantic v2 deprecations** (`class Config`, `on_event` startup) in `server.py` — tech debt,
  not blocking; clean up opportunistically.
- **`main` is 105 commits behind `ApolloV3`** — don't treat it as the trunk; promotion flow is
  `feature → staging → ApolloV3`.
- **Branch protection is NOT set yet** — `staging`/`ApolloV3` are unprotected (Phase 2).

---

## 10. References
- `docs/TESTING-CI-PLAN.md` — full phased plan, gap matrix, reference configs (on
  `test/phase0-stabilization`; reaches `staging` once merged).
- `.feller/tasks/2026-06-09-backend-testing-ci/RESEARCH-ARCHIVE.md` — cited best-practices
  research (pytest/pgvector, GitHub Actions, git staging strategy).
- Architecture + test-audit details are summarized in the plan doc §1 and the gap matrix §5.
