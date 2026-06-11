# Backend Testing System + Staging Branch + CI — Execution Plan

> **Status:** Plan only (no code/CI changes yet). **Ambition:** Balanced from day one
> (~65–70% coverage gate, full pyramid + regression). **Scope:** the whole
> `ai-ta-backend` (FastAPI · async SQLAlchemy · pgvector · OpenAI · Neo4j · Supabase).
> **Author:** feller research+recon fan-out (3 web-research agents + 2 codebase agents), 2026-06-09.

---

## 0. Why this plan looks the way it does

This is **not greenfield**. The backend already has 24 test files, a `conftest` that
stubs OpenAI + Supabase, and a CI workflow. The problem is that **almost none of it
actually works as a safety net**:

**Measured reality** (`.venv` Python, `pytest`, run 2026-06-09):

```
START:        113 passed · 5 failed · 2 skipped · 4 errors   (collection abort)
AFTER PHASE 0: 117 passed · 7 skipped · 0 failed · 0 errors  ✅ COMPLETE
```

> ⚠️ An earlier audit reported "49 passed / 62 errors" — that run used the **system
> Python without the project `.venv`**, so everything importing `fastapi`/`pydantic`
> looked like an error. There was **no `server.py` import crisis.** The true breakage was
> small and is now fixed (Phase 0 below).

Original real problems (all resolved or explicitly deferred in Phase 0):
- **4 failures** — `test_tutor_prompt.py` brittle/stale assertions (prompt text evolved;
  one assertion even contradicted its own comment). → fixed to check rule intent.
- **1 failure** — `test_knowledge_stores.py` over-mocked an exact `run_async` call
  sequence that drifted (`assert 0 >= 100`). → quarantined for a Phase-4 AsyncClient rewrite.
- **4 errors** — `test_workspaces.py` (1, imports a function deleted in the migration →
  obsolete, skipped) + `test_migration_015.py` (3, need real Postgres → `db_session`
  skip fixture added; real fixture in Phase 1). A single collection error was aborting
  the *entire* run.
- **markers unregistered** — `@pytest.mark.unit/integration` produced warnings and gave no
  filtering. → registered + `--strict-markers`.
- **async tests** — `pytest-asyncio` installed but `asyncio_mode` unset. → set to `auto`.
- **CI was decorative** — `.github/workflows/main.yml` ended every step with `|| true`,
  triggered only on `main`. → replaced with an honest gating workflow on
  `main`/`staging`/`production`.
- **pgvector still untestable** — all DB tests run on `sqlite+aiosqlite:///:memory:`;
  pgvector/HNSW and the distance operators (`<=>`, `<->`) **do not exist in SQLite**.
  Real Postgres harness lands in **Phase 1** (the core remaining work).

**Therefore the plan front-loads repair and infrastructure.** Writing 200 new tests on
top of a suite that doesn't run, a CI that can't fail, and a DB layer that can't see
pgvector would be building on sand. Phases 0–2 make the foundation real; Phases 3–5 fill
coverage; Phase 6 ratchets the gate.

---

## 1. Target test taxonomy (types fit for early stage)

We adopt the **Testing Trophy** (static → integration-heavy), not the classic 70%-unit
pyramid — modern tooling (httpx ASGITransport, Testcontainers) makes integration tests
cheap, and integration is where this RAG backend's bugs actually live.

| Type | What it means HERE | Speed / where it runs | Target mix |
|------|--------------------|-----------------------|------------|
| **Static** | `ruff` (lint+format) + `mypy` | every PR, seconds | foundational |
| **Unit** | Pure logic, no DB/network: citation formatting, prompt assembly, router/intent decisions, chunking, RRF fusion math, reranker scoring, sympy solver, misconception rules, Pydantic contracts | every PR, `-m unit`, < 30s | ~25% |
| **Integration** | Endpoints via `httpx.AsyncClient` + real Postgres/pgvector (Testcontainers), with **LLM & embeddings mocked**: retrieval pipeline, indexing, knowledge CRUD, chats/memory, auth enforcement, DB models/migrations, apollo handlers (real Neo4j container) | PR-to-staging + nightly, `-m integration` | **~55% (the bulk)** |
| **E2E** | 3–6 revenue-critical journeys end-to-end on synthetic data: teacher upload→index→searchable; student ask→retrieve→answer+citations; apollo session lifecycle | merge-to-staging + nightly, `-m e2e` | thin cap (3–6 tests) |
| **Regression** | A permanent test for every fixed bug (RED before fix → GREEN after) + golden/snapshot tests for **deterministic** structured outputs (API envelopes, chunking results, fake-embedding retrieval ordering) | every PR (they're just tagged unit/integration) | grows organically |
| **Smoke** | Post-deploy health/`/healthz` + one happy-path request against the deployed env | post-deploy only | 1–3 checks |
| **Eval (deferred to nightly)** | RAG quality vs a golden Q→A set scored by DeepEval/RAGAS thresholds. **Never on the PR path** (nondeterministic, costs money, hits real models) | `nightly.yml` or `eval` label | separate gate |
| **Contract (DEFER)** | Pin the shape of OpenAI/Supabase/Neo4j responses against the real sandbox. Negative ROI pre-revenue — **revisit when you have paying customers.** | — | not now |

**The single most important architectural decision:** test the DB layer against the
**real `pgvector/pgvector` image** (Testcontainers locally, GH Actions `services:` in CI),
with **deterministic fake embeddings** (fixed vectors keyed by input hash) and **mocked /
replayed LLM calls**. This keeps CI fast, free, and deterministic while actually
exercising pgvector ordering and HNSW.

---

## 2. Target tooling stack

| Concern | Choice | Notes |
|---|---|---|
| Test runner | **pytest ≥8** | already present |
| Async | **pytest-asyncio** `asyncio_mode = auto` | *currently the #1 silent bug* — configure it |
| HTTP-app tests | **httpx `AsyncClient` + `ASGITransport`** | NOT `TestClient` (breaks on asyncpg's event loop) |
| DB tests | **Testcontainers `pgvector/pgvector:pg16`** (local) + GH `services:` (CI) | session-scoped engine + function-scoped transactional rollback |
| External HTTP mock | **respx** | OpenAI/Supabase SDKs are httpx-based |
| LLM determinism | static fake clients (unit) + **VCR.py cassettes** `record_mode=none` (integration) | filter `authorization`/`x-api-key` headers; `match_on` body |
| Fake embeddings | hand-rolled deterministic vector fn | reproducible retrieval ordering |
| Data builders | **factory_boy** | over hand-built dicts; never assert on generated IDs |
| Property tests | **hypothesis** | for chunker / parser / ranking edge cases |
| Coverage | **pytest-cov** + **diff-cover** (or Codecov) | branch coverage; patch gate on new code |
| Parallelism | **pytest-xdist** `-n auto` | `pytest -n auto --cov` (never `coverage run -m pytest -n`) |
| Safety nets | **pytest-timeout**, **pytest-randomly** | LLM-path tests must fail fast, not hang |
| Lint+format | **ruff** | replaces flake8/black/isort |
| Types | **mypy** (start non-strict) | |
| Package/install | **uv** (`astral-sh/setup-uv`, `enable-cache`) | 10–100× faster CI installs; also fixes the "no lockfile" drift |
| Pre-commit | ruff + ruff-format hooks | same config local + CI |

New file `requirements-test.txt` (or a `[project.optional-dependencies] test` group):
`pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-mock`, `pytest-xdist`, `pytest-timeout`,
`pytest-randomly`, `respx`, `vcrpy`, `freezegun`, `hypothesis`, `factory-boy`,
`testcontainers[postgres]`, `diff-cover`, `ruff`, `mypy`.

---

## 3. Git branching + CI gate model

**Recommended model: GitLab-Flow environment branches**, lightest form. Downstream-only
promotion, never the reverse. This is the smallest model that gives a real pre-prod gate
without Git-Flow ceremony, and it's the only common model where the environment is an
actual **branch** (which is what was asked for).

```
feature/* ──(squash PR)──▶ main ──(merge PR)──▶ staging ──(merge PR)──▶ production
   ▲   branch from           │ deploy: dev/preview │ deploy: staging env   │ deploy: PROD
   │   up-to-date main       │ CI GATE 1           │ CI GATE 2             │ (gated reviewer)
   └─ short-lived            │ lint·type·UNIT      │ FULL integration+e2e  │ CI GATE 3
                             │ (<5 min)            │ + migration dry-run   │ smoke + health only
                                                   │ nightly: regression+eval
HOTFIX: fix on feature ▶ main FIRST (upstream-first) ▶ promote downstream
        (admin break-glass on production only for live incidents, then back-merge to main)
```

**ApolloV3 today is the live prod line.** Recommendation: **rename `ApolloV3` → `production`**
(GitHub auto-redirects old refs and open PRs) so the prod branch has a conventional name CI
configs expect. Lower-touch alternative: keep `ApolloV3`, just pin it as the `production`
environment's only allowed deploy branch. Either is fine; renaming is cleaner long-term.

**Which suite runs at which gate** (avoid paying for the same suite twice):

| Trigger | Suite | Budget |
|---|---|---|
| PR → `main` / `staging` | lint · mypy · **unit** + fast integration · patch-coverage | < 5 min |
| push `staging` (post-merge) | full **integration + e2e** + Alembic migration dry-run on a staging DB | heavy, OK |
| `staging` nightly (cron) | full regression + **RAG eval** + `pip-audit` + strict coverage floor | unbounded |
| push `production` | deploy (gated) + **smoke/health only** | minimal |

**Branch protection (small-team calibrated, via Rulesets):** require the single
**`ci-passed`** aggregation status check + 1 review on `main`/`production` (loosen `staging`
to 0 reviews if solo), require branches up-to-date, linear history (squash into `main`),
block force-push/deletion. Don't require 2 reviewers on a ≤2-person team — it deadlocks;
lean on required CI checks as the gate.

**GitHub Environments:** `dev/preview`, `staging`, `production`, with **prod secrets scoped
to the `production` environment only** (so a staging workflow physically can't read them).
⚠️ **Plan caveat:** Environment *required reviewers* / wait timers on **private** repos need
GitHub **Enterprise** (not Free/Pro/Team). If this repo is private on a lower tier, gate prod
with a branch-protection review + a manual `workflow_dispatch` approval job
(`trstringer/manual-approval`) instead. **Verify the repo's plan in Phase 2.**

---

## 4. Execution roadmap (phased)

Effort estimates assume one engineer; they're relative, not commitments. Each phase has a
hard **exit criterion** — don't start the next until it's met.

### Phase 0 — Stabilize: make what exists run, make CI honest  ✅ **DONE (2026-06-09)**

The unblock. No new feature tests yet — just get a true signal.
**Result:** 117 passed / 7 skipped / 0 fail / 0 error. Changes: `pytest.ini` (markers +
`asyncio_mode=auto` + `--strict-markers`), `tests/conftest.py` (`db_session` skip fixture),
`test_tutor_prompt.py` (4 assertions de-brittled), `test_knowledge_stores.py` (quarantined),
`test_workspaces.py` (obsolete → module skip), `.github/workflows/main.yml` (honest gate).

| Task | Detail |
|---|---|
| 0.1 Fix collection errors | Diagnose the 62 import errors. `tests/integration/*` can't import `server.py` — likely heavy import-time side effects (the startup env validation, client instantiation at module load). Make the app importable under test (lazy clients / `create_app()` factory, or guard side effects behind `if __name__`). |
| 0.2 Fix the 4 failures | `test_tutor_prompt.py` imports a moved/renamed prompt module — fix the import or the test. |
| 0.3 Configure pytest-asyncio | Add `asyncio_mode = auto` → unblocks the 8 skipped router tests immediately. |
| 0.4 Central pytest config | Migrate `pytest.ini` → `[tool.pytest.ini_options]` in `pyproject.toml`: `--strict-markers --strict-config --import-mode=importlib`, register markers `unit/integration/e2e/slow/llm`, `testpaths=tests`. |
| 0.5 Fix `test_workspaces.py` | Broken import (`build_local_static_workspace_config` moved/deleted) — repair or delete. |
| 0.6 Quarantine, don't delete | Tag genuinely-can't-run-yet tests `@pytest.mark.skip(reason=...)` or `@pytest.mark.integration` so the **green set is honest and required**, broken set is visible backlog. |
| 0.7 Honest CI (minimal) | New `ci.yml`: remove every `|| true`; run `pytest -m "not integration and not e2e" -q` as a **required** check; triggers on PR to `main` + `staging`. Keep it small but real. |

**Exit criterion:** `pytest -m "unit"` is green and gating in CI; the count of
quarantined tests is written down. No more silent skips.

---

### Phase 1 — Test harness & infrastructure  *(≈1 week)*

Build the fixtures everything else depends on.

| Task | Detail |
|---|---|
| 1.1 Real-Postgres fixture | Session-scoped `pg_container` via `PostgresContainer("pgvector/pgvector:pg16")`; async engine with `NullPool`; `CREATE EXTENSION vector` + `Base.metadata.create_all` (or `alembic upgrade head`); register pgvector type on asyncpg connect. |
| 1.2 Transactional isolation | Function-scoped `db_session` fixture: connection → outer txn → session bound to it → SAVEPOINT re-open on `after_transaction_end` → rollback in teardown. Pristine DB per test, fast. Keep a *small* set of non-transactional tests for HNSW-index/recall assertions. |
| 1.3 Async API client | `client` fixture: `AsyncClient(transport=ASGITransport(app=app))`; override `get_db_session`, current-user/auth, and the OpenAI/embedding dependency; **`app.dependency_overrides.clear()` in teardown**. Wrap lifespan with `asgi-lifespan` if startup hooks matter. |
| 1.4 Deterministic fakes | `fake_embeddings(text) -> vector` (hash-seeded, fixed dim = `EMBEDDING_DIM`); fake OpenAI chat client returning canned structured responses; promote the existing `conftest` stubs into a real `tests/fakes/` package. |
| 1.5 LLM replay (integration) | VCR.py config: `record_mode="none"` in CI, `filter_headers=["authorization","x-api-key"]`, `match_on=[..., "body"]`; a `make rerecord` target run weekly. For agent/tool-call loops prefer a small programmable fake server over cassettes. |
| 1.6 Factories | `tests/factories/` with factory_boy builders for `SearchSpace`, `AITADocument`, `ChatSession`, `CourseMembership`, `TeacherUpload`, apollo models. |
| 1.7 Layout & conftest hygiene | Reorganize to `tests/{unit,integration,e2e}/` each with a nested `conftest.py` so the DB container never spins up for a pure unit run. De-duplicate the two overlapping `conftest.py` Supabase mocks into one shared module. |
| 1.8 Coverage wiring | `[tool.coverage.run] relative_files=true, branch=true, source=["."], omit=[migrations, server bootstrap, __init__]`; `--cov-report=xml`. Wire `diff-cover` (compare against `origin/staging`). |
| 1.9 Neo4j test container | For apollo integration: Testcontainers Neo4j fixture (prod uses Neo4j Aura); mock it in unit tests. |

**Exit criterion:** a sample integration test hits real pgvector (asserts distance
ordering on known fake vectors) and a sample endpoint test runs through `AsyncClient` with
a rolled-back DB — both green locally and in CI's Postgres service.

---

### Phase 2 — Staging branch + full CI/CD wiring  *(≈1 week)*

| Task | Detail |
|---|---|
| 2.1 Reconcile branches | Ensure `main ⊇ production` content; decide `ApolloV3` → `production` rename vs alias (§3). One-time merge/cherry-pick if `main` lags prod. |
| 2.2 Create `staging` | `git switch main && git pull && git switch -c staging && git push -u origin staging` (starts identical to `main`, zero drift). |
| 2.3 Composite setup action | `.github/actions/setup/action.yml`: `setup-uv` (cache) + `uv sync --locked`. DRY across jobs. |
| 2.4 `ci.yml` (PR gate) | Parallel jobs `quality` (ruff+mypy), `unit` (matrix 3.11/3.12, `pytest -m unit -n auto --cov`), `integration` (`services: pgvector/pgvector:pg16`, `alembic upgrade head`, `pytest -m integration -n auto`), then a `ci-passed` aggregation job (`if: always()`, checks all `needs.*.result`). `permissions: {}` top-level; `concurrency` with `cancel-in-progress` PR-only; `paths-ignore` for docs. |
| 2.5 `nightly.yml` | `schedule: cron '0 6 * * *'` + `workflow_dispatch`: full suite incl. e2e, strict `--cov-fail-under`, `pip-audit`, RAG eval. |
| 2.6 Coverage gate | Project floor **non-blocking, ratcheted** (`target: auto`, no >1% regression); **patch coverage ≥95% on new code blocking** (diff-cover or Codecov `patch.target`; started at 80%, raised to 95% on 2026-06-11 as workspace-wide strict rule). Start project floor at the measured baseline, never let it drop. |
| 2.7 Branch protection | Rulesets on `main`/`staging`/`production`: required check = `ci-passed`, reviews per §3, linear history, no force-push. Configure via `gh api .../rulesets` (IaC-friendly). |
| 2.8 Environments + secrets | Three GH Environments; move prod secrets into `production` env scope; verify plan tier for required-reviewer support (else manual-approval fallback). |
| 2.9 Supply-chain hygiene | SHA-pin all actions with `# vX.Y.Z` comments; `dependabot.yml` for `github-actions` + `pip`/`uv`; least-privilege `permissions:` per job. |
| 2.10 Pre-commit | `.pre-commit-config.yaml` (ruff + ruff-format); run via `pre-commit/action` in the `quality` job so local == CI. |
| 2.11 Dry-run | Push one trivial feature through `feature → main → staging → production` to prove the gates and promotion flow before relying on it. |

**Exit criterion:** a PR to `staging` runs lint+type+unit+integration against real Postgres,
blocks on red, blocks on <95% patch coverage, and the promotion chain works end-to-end.

---

### Phase 3 — Fill the unit layer (pure logic)  *(≈1–2 weeks)*

Highest-leverage, fastest tests first. Targets (from the architecture map's testable seams):

- `citations/formatter.py` (extend edge cases), `config/contracts.py` (Pydantic models)
- `ai/main_ai.py` — keyword extraction, query normalization, snippet scoring, relevance
  graduation, LLM-failure fallbacks (assert fallback **values**, not just shape)
- `ai/router/*` — embedding-router margin/abstain, LLM-router structured output,
  orchestrator Stage1→Stage2 fallback (now that asyncio is configured)
- `retrieval/` pure bits — RRF fusion math (`hybrid_search` scoring fn), `reranker.py`
  scoring/dedup, `store_bias.py` weights, `context_packer.py` token budgeting
- `indexing/document_chunker.py` — chunking strategies (good hypothesis target)
- `apollo/parser/*` (equation/concept extraction with mocked LLM), `apollo/solver/forward_chain.py`
  + `sympy_exec.py`, `apollo/overseer/misconception_bank.py` (rule-based)
- `ai/vision.py` — GPT-4V→fallback path with mocked client
- `auth.py` — JWT/claims parsing, token cache, membership checks (pure parts)

**Exit criterion:** unit layer covers the pure-logic seams above; project coverage rises
measurably; no new code merges below the patch gate.

---

### Phase 4 — Integration layer (real DB + mocked LLM)  *(≈2–3 weeks)*

The bulk of the trophy. All via `AsyncClient` + rolled-back real Postgres + fakes.

- **Server endpoints** (revive the ~56 quarantined `test_e2e_api.py` tests, rewritten to
  `AsyncClient` + dependency overrides): `/ask` + `/ask/stream`, `/knowledge/*`,
  `/teacher/*` (upload/retry/weights), `/classes`, `/invite-links/*`, `/healthz`.
  Assert **behavior** (auth 401/403, validation 422, correct data), not just `"answer" in json`.
- **Retrieval** — `retrieval/pipeline.py` + `hybrid_search.py` against real pgvector with
  known fake vectors: assert ranking order, RRF fusion, store-bias effects, reranking.
- **Indexing** — chunk → fake-embed → persist `AITADocument` → searchable round-trip.
- **Knowledge** — `knowledge/manager.py` async CRUD, search-space integration.
- **Chats** — `chats/service.py` session/turn persistence + memory summarization triggers.
- **Auth** — enforcement on endpoints (valid/invalid/missing token, membership gates).
- **Database** — models + migration 015/021 against **real Postgres** (revive
  `test_migration_015.py` with the new `db_session` fixture).
- **Apollo handlers** — `apollo/handlers/chat.py`, `next.py`, lifecycle against
  Testcontainers Neo4j + mocked LLM parser/narrator.

**Exit criterion:** every external boundary (DB, embeddings, LLM, auth, Neo4j) has ≥1
happy-path + ≥1 primary-failure integration test; overall coverage ≥ the balanced gate
(~65–70%).

---

### Phase 5 — E2E + regression + eval  *(≈1–2 weeks)*

- **E2E (3–6 only):** (A) teacher upload → OCR/index → embeddings → searchable;
  (B) student ask → router → retrieval → answer + citations; (C) apollo session
  from_hoot → chat → next → progress. Synthetic data, run on merge-to-staging + nightly.
- **Regression harness:** convention that every fixed bug ships a repro test; a
  `tests/regression/` home; golden/snapshot tests **only** for deterministic structured
  outputs (API envelope, chunking result, fake-embedding retrieval ordering) — **never**
  snapshot raw LLM text.
- **RAG eval (nightly, separate gate):** fixed golden Q→A/expected-docs set scored by
  DeepEval/RAGAS faithfulness/answer-relevancy/contextual-precision with thresholds.
  Catches prompt/model regressions without polluting the PR path.

**Exit criterion:** the three critical journeys are covered E2E and run nightly; a sample
bug-repro regression test exists as the template.

---

### Phase 6 — Ratchet & harden  *(ongoing)*

- Bump the project coverage floor toward target as it organically rises (ratchet, never drop).
- Flaky-test policy: quarantine within 24h, fix or delete — don't let flake erode trust.
- Keep PR CI under ~5 min (unit+fast-integration); push slow suites to nightly.
- Document the flow in `CLAUDE.md` + `docs/DATA-FLOW.md`; onboard the team to the
  feature→main→staging→production promotion + upstream-first hotfix rules.
- Revisit deferred **contract tests** once there are paying users.

---

## 5. Gap matrix (module → today → target)

| Module | Today | Target test types |
|---|---|---|
| `citations/formatter.py` | ~80% unit | unit (edge cases) |
| `config/contracts.py`, `config/settings.py` | partial | unit |
| `ai/main_ai.py` | ~40% unit (happy path) | unit (failure modes, token limits, citation logic) |
| `ai/orchestrator.py` | **0%** | unit (flow) + integration (full `/ask`) |
| `ai/vision.py` | **0%** | unit (GPT-4V→fallback, error paths) |
| `ai/router/*` | ~40% but **skipped** | unit (unblock asyncio) + integration (Stage1→2) |
| `retrieval/hybrid_search.py` | **0%** | unit (fusion math) + integration (real pgvector) |
| `retrieval/reranker.py`, `store_bias.py`, `context_packer.py` | ~partial | unit + integration |
| `retrieval/pipeline.py` | **0%** | integration (real DB + fakes) |
| `indexing/*` (6 modules) | **0%** | unit (chunking) + integration (embed→persist round-trip) |
| `indexers/*` | **0%** | unit + integration |
| `knowledge/manager.py`, `teacher_weekly.py` | ~20% | integration (async CRUD, ingestion) |
| `chats/service.py` | **0%** | unit (memory) + integration (persistence) + e2e (multi-turn) |
| `auth.py` | **0%** | unit (parsing) + integration (enforcement on endpoints) |
| `database/models.py`, migrations | schema-only, **can't run** | integration (real Postgres) |
| `reports/ai_use/*` | ~40% | unit (assembly/redaction) + integration (routes) |
| `apollo/parser/*`, `solver/*`, `overseer/*` | **0%** | unit (pure logic, mocked LLM) |
| `apollo/handlers/*`, `persistence/*` | **0%** | integration (Neo4j container + mocked LLM) |
| `server.py` endpoints | ~0% (56 tests **error**) | integration via `AsyncClient` (revive + rewrite) |
| `workspaces/manager.py` | ~0% (**broken import**) | unit + integration |

---

## 6. Reference configs (drop-in starting points)

> Illustrative; finalize during execution.

**`pyproject.toml` (test + coverage):**
```toml
[tool.pytest.ini_options]
addopts = ["-ra", "--strict-markers", "--strict-config", "--import-mode=importlib"]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
  "unit: no DB, no network",
  "integration: real DB / real HTTP wiring (LLM mocked)",
  "e2e: full critical-path flows",
  "slow: > 1s",
  "llm: exercises an LLM/embedding path (mocked/replayed)",
]

[tool.coverage.run]
relative_files = true
branch = true
source = ["."]
omit = ["*/migrations/*", "*/tests/*", "*/__init__.py", "server.py"]   # tune bootstrap omits
[tool.coverage.report]
exclude_lines = ["pragma: no cover", "if TYPE_CHECKING:", "if __name__ ==", "@overload"]
```

**`ci.yml` (PR gate) — skeleton** (SHA-pin every action in real use):
```yaml
name: CI
on:
  pull_request: { branches: [staging, main], paths-ignore: ['**.md', 'docs/**'] }
  push: { branches: [staging, main] }
  workflow_dispatch:
permissions: {}
concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: ${{ github.event_name == 'pull_request' }} }
jobs:
  quality:
    runs-on: ubuntu-latest
    permissions: { contents: read }
    steps:
      - uses: actions/checkout@<sha>          # v4.2.2
      - uses: ./.github/actions/setup
      - run: uv run ruff check --output-format=github .
      - run: uv run ruff format --check .
      - run: uv run mypy .
  unit:
    runs-on: ubuntu-latest
    permissions: { contents: read }
    strategy: { fail-fast: false, matrix: { python-version: ['3.11','3.12'] } }
    steps:
      - uses: actions/checkout@<sha>
      - uses: ./.github/actions/setup
      - run: uv run pytest -m unit -n auto --cov=. --cov-report=xml
  integration:
    runs-on: ubuntu-latest
    permissions: { contents: read }
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: app_test }
        ports: ['5432:5432']
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
    env: { SUPABASE_DB_URL: postgresql+asyncpg://postgres:postgres@localhost:5432/app_test }
    steps:
      - uses: actions/checkout@<sha>
      - uses: ./.github/actions/setup
      - run: uv run alembic upgrade head      # or create_all in fixture
      - run: uv run pytest -m integration -n auto --cov=. --cov-report=xml
      - run: uv run diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
  ci-passed:
    if: always()
    needs: [quality, unit, integration]
    runs-on: ubuntu-latest
    steps:
      - run: |
          [ "${{ needs.quality.result }}" = success ] && \
          [ "${{ needs.unit.result }}" = success ] && \
          [ "${{ needs.integration.result }}" = success ] || exit 1
```

**Real-pgvector + transactional fixture (conftest skeleton):**
```python
@pytest.fixture(scope="session")
def pg_container():
    with PostgresContainer("pgvector/pgvector:pg16") as c:
        yield c

@pytest_asyncio.fixture(scope="session")
async def engine(pg_container):
    url = pg_container.get_connection_url().replace("postgresql://", "postgresql+asyncpg://")
    eng = create_async_engine(url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()

@pytest_asyncio.fixture
async def db_session(engine):
    async with engine.connect() as conn:
        trans = await conn.begin()
        Session = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with Session() as s:
            await s.begin_nested()
            @event.listens_for(s.sync_session, "after_transaction_end")
            def _restart(sess, txn):
                if txn.nested and not txn._parent.nested:
                    sess.begin_nested()
            yield s
        await trans.rollback()
```

---

## 7. Risks & sequencing notes

- **`server.py` import-time side effects are the critical-path blocker.** The 62 errors
  almost certainly stem from clients/validation running at import. A `create_app()` factory
  + lazy client init pays off everywhere (testability, faster cold start). Tackle in Phase 0.
- **pgvector ≠ SQLite** — do not let anyone "fix" DB tests by leaning harder on the SQLite
  mock; it hides real bugs. Real Postgres is non-negotiable for the retrieval layer.
- **Don't gate PRs on LLM calls or eval** — keep them nightly. PR CI must be deterministic,
  free, and < 5 min or engineers will `.skip` to merge.
- **Private-repo Environment plan gate** (§3) — verify before designing prod approval.
- **Coverage gate placement** — gate **patch** coverage (new code), ratchet the project
  floor. A flat repo-wide floor on a 40–50% codebase blocks every hotfix and trains the
  team to disable coverage.
- **Over-mocking is the existing suite's biggest smell** (tests that pass even if code
  breaks). New tests assert observable behavior/values, use real DB, mock only LLM/embeddings.

---

## 8. Success criteria (definition of done for the whole effort)

- [ ] `pytest` runs clean — 0 errors, 0 silent skips; every test either runs or is
      explicitly + traceably quarantined.
- [ ] CI gates for real: red suite blocks merge; PR CI < 5 min; integration runs on real
      pgvector; patch coverage ≥95% enforced; project floor ratcheted from baseline.
- [ ] `staging` branch exists with its own Actions; `feature→main→staging→production`
      promotion proven end-to-end; branch protection + environments configured.
- [ ] Every external boundary has happy-path + failure integration coverage.
- [ ] 3–6 E2E journeys + a regression convention + nightly RAG eval in place.
- [ ] Overall coverage at the balanced target (~65–70%), branch-measured.
- [ ] Flow documented in `CLAUDE.md` / `DATA-FLOW.md`.

---

## Appendix — source briefs

Full citation-backed research (test best-practices, GitHub Actions CI, git staging
strategy) and the raw architecture map + test audit are archived under
`.feller/tasks/2026-06-09-backend-testing-ci/`.
