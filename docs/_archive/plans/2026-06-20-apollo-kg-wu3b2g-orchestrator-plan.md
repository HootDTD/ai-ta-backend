# WU-3B2g — Stage-0 trigger + worker shell + 6-stage orchestrator + observability + lease-reaper (TDD plan)

**Date:** 2026-06-20
**Unit:** WU-3B2g (the §8B orchestration spine — the LAST wiring unit of WU-3B2).
**Branch (already checked out — do NOT create/switch/push):** `feat/apollo-kg-wu3b2g-orchestrator`.
**diff-cover compare branch:** `feat/apollo-kg-wu3b2f-queue-metered` (verified present, tip `475625c`).
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §8B.1/8B.2/8B.6 + §9 OPS-2/OPS-5/SPEC-1.
**Contract:** `docs/superpowers/plans/2026-06-19-apollo-kg-wu3b2-split-proposal.md` ### WU-3B2g.
**Planner class:** feller-planner-ai-pipeline (idempotency + failure-path declarations are the whole job).

---

## 0. One-paragraph summary

WU-3B2g WIRES the already-built §8B stages (3B2b–3B2f) into the upload pipeline. It adds a THIN content-key-idempotent transactional enqueue at the indexing-completion seam (`knowledge/teacher_weekly.py`), a dormant flag-gated worker SHELL (`apollo/provision_worker.py`, mirroring the frozen `apollo/learner_janitor_worker.py`), the 6-stage per-document ORCHESTRATOR (`apollo/provisioning/orchestrator.py`), the promotion step (`apollo/provisioning/promote.py`, calling the frozen `run_promotion_lint` + `project_canon`), the enqueue helper (`apollo/provisioning/enqueue.py`), every observability write (`apollo_ingest_runs` / `apollo_rejected_problems` / `apollo_ingest_errors`), the lease-reaper / terminal-status reconciliation, and a 4th `Procfile` line. It REDEFINES no stage and changes `apollo/handlers/done.py` NOT AT ALL. The load-bearing non-regression: with `APOLLO_AUTOPROVISION_ENABLED` OFF the trigger ENQUEUES but no worker drains, so teacher uploads behave EXACTLY as today.

---

## 1. Pipeline shape diagram (per-box owner + retry + failure mode)

```
[teacher PDF upload]
  → [teacher_upload_worker drains TeacherUploadJob (EXISTING, frozen)]
  → [_index_existing_upload_async finalize session flips upload READY + job COMPLETED (teacher_weekly.py:1131-1174, EXISTING)]
  → [THIN enqueue_provisioning_job(...) INSIDE that same session  ← WU-3B2g EDIT]
  → [apollo_provisioning_jobs row (state='pending') + apollo_ingest_runs row (status='queued')]
  → [apollo-provision worker SHELL polls (DORMANT; APOLLO_AUTOPROVISION_ENABLED OFF)  ← WU-3B2g NEW]
  → [claim_provisioning_job SKIP-LOCKED (3B2f, frozen) → ClaimedJob]
  → [run_provisioning(db, neo, job, metered_chat): 6 stages per document  ← WU-3B2g NEW]
       scrape (3B2d) → find_or_generate (3B2e) → validate_pair (3B2e)
       → tag_and_mint (3B2d) [dedup resolve_candidate 3B2c inside] → promote (run_promotion_lint 3B2b + project_canon)
  → [apollo_concept_problems tier 1→2 + payload.reference_solution + :Canon rebuild]
  → [list_problems_for_concept tier==2 filter (3B2a, frozen) → student teaching session]
```

| Box | Owner primitive | Retry | Failure mode |
|---|---|---|---|
| Indexing finalize session | `teacher_weekly._index_existing_upload_async` finalize block (`:1131-1174`) — Postgres txn | EXISTING teacher-upload lease/retry | unchanged; the enqueue rides the SAME commit so a job is never visible for a doc that didn't finish indexing |
| Enqueue | `apollo/provisioning/enqueue.py::enqueue_provisioning_job` (NEW); table `apollo_provisioning_jobs` partial-unique-index `WHERE state IN ('pending','running')` (migration 030, frozen) | content-hash short-circuit (skip) + partial-unique-index (collapse) | a duplicate open job is collapsed by the index; an unchanged re-upload is a no-op |
| Queue claim | `apollo/provisioning/queue.py::claim_provisioning_job` SKIP-LOCKED (3B2f, FROZEN) | lease-expiry re-claim, `attempt_count` bump | a crashed claimer leaves `running` + expired lease → re-claimable, OR the lease-reaper re-opens it |
| Orchestrator stages | `apollo/provisioning/orchestrator.py::run_provisioning` (NEW) | per-stage error → `apollo_ingest_errors` row; terminal → run `status='failed'` + job `'failed'` via `fail_job` | NEVER leaves the run `'running'` (would wedge the partial-unique-index against re-enqueue — §9 OPS-5) |
| Promote | `apollo/provisioning/promote.py::promote` (NEW); `run_promotion_lint` (3B2b) + `project_canon` (3C1) frozen | re-run idempotent (tier flip + MERGE projection) | lint FAIL → `apollo_rejected_problems` row, problem stays Tier-1 (never reaches a student) |
| Worker SHELL + lease-reaper | `apollo/provision_worker.py` (NEW), mirrors `learner_janitor_worker.py` | per-iteration flag read; lease-reaper pass each sweep | flag OFF → NO drain (byte-identical upload behavior); one bad sweep logs + survives, next sweep continues |
| Process line | `Procfile` `apollo-provision:` (EDIT); scaled to 0 replicas while dormant | n/a | n/a — activation is a human deploy step |

## 2. Idempotency declaration

Three NESTED idempotency layers, each with its own key, all enforced by FROZEN upstream code or content hashes — never auto-increment IDs. This unit WIRES them; it must not introduce a fourth ad-hoc scheme.

### 2a. Document-level no-op (enqueue short-circuit) — §9 OPS-2
- **Key:** `(document_id, content_hash)` where `content_hash = AITADocument.content_hash` (the existing per-document SHA-256-ish hash, `database/models.py:135`, computed by `compute_content_hash`). Path-independent, survives a re-index (the volatile `aita_chunks.id` is NEVER used).
- **Duplicate handling:** SKIP. `enqueue_provisioning_job` first SELECTs `apollo_ingest_runs` for a `status='succeeded'` row with the same `(document_id, content_hash)`; if found, return WITHOUT inserting a job (an unchanged re-upload enqueues ZERO new jobs).
- **Partial-progress recovery:** the `apollo_provisioning_jobs_open_uniq` partial-unique-index (`WHERE state IN ('pending','running')`, migration 030) collapses a duplicate OPEN job if the short-circuit was missed (e.g. the first run is still `running`). The enqueue catches `IntegrityError` on that index → treat as "already enqueued", no-op.

### 2b. Job-level dedup (the queue) — frozen 3B2f
- **Key:** at most one OPEN (`pending`/`running`) job per `document_id` (the partial-unique-index).
- **Duplicate handling:** the index rejects a second open insert; `claim_provisioning_job` (FROZEN) hands exactly one worker the row under SKIP-LOCKED.
- **Partial-progress recovery:** an expired lease (`lease_expires_at < now()`) makes a stuck `running` row re-claimable; `fail_job`/`complete_job` (FROZEN) move it terminal.

### 2c. Intra-job no-op (re-run skips already-scraped chunks) — §9 SPEC-1/OPS-2
- **Key:** `(concept_id, problem_code)` where `problem_code = f"scrape.{chunk_content_hash}"` and `chunk_content_hash = sha256(normalize(chunk.content))` (FROZEN in `scrape.py::_problem_code_for` / `_chunk_content_hash`).
- **Duplicate handling:** `write_tier1_problems` (FROZEN) SELECT-then-skips a `(concept_id, problem_code)` that already exists → a re-run of a completed job inserts ZERO new Tier-1 rows.
- **Partial-progress recovery:** the orchestrator drives stages over the Tier-1 rows; a crashed-then-re-claimed job re-scrapes (no new rows), re-promotes (tier flip + `:Canon` MERGE are idempotent), so partial progress is replay-safe. The promote step keys the tier-flip on the existing `apollo_concept_problems` row, never a new insert.

**No appended strings, no incremented counters as keys.** The only counters written (`apollo_ingest_runs.n_promoted` etc.) are per-RUN aggregates on a fresh row per job, not idempotency keys; a re-claimed job re-uses its existing run row (`ClaimedJob.ingest_run_id`) and the orchestrator RESETS the per-run counters to a freshly-computed value (assign, not `+=`) so a replay does not inflate them.

## 3. Model & cost declaration

WU-3B2g makes **no NEW LLM call** of its own — it ORCHESTRATES the stage calls, all of which route through the FROZEN `MeteredChat` (3B2f). Per-PR tests use MOCKED chat/embed/judge callables (Tier-1, no network). The cost surface this unit governs is therefore the **per-document aggregate + the circuit-breaker abort**, not per-call pricing (3B2f owns that).

| Stage call (routed via MeteredChat) | Model (resolved by MeteredChat) | Tier | Per-PR cost |
|---|---|---|---|
| scrape (`scrape.py`, one cheap call per chunk) | `APOLLO_CHEAP_MODEL` default `gpt-4o-mini` | cheap | $0 (mocked `chat_fn`) |
| find_or_generate (`solution.py`) | `MAIN_MODEL` default `gpt-4o` | main | $0 (mocked) |
| validate_pair Phase A/B (`pairing_gate.py`) | cheap | cheap | $0 (mocked) |
| tag_and_mint concept tag (`tag_mint.py`) | cheap | cheap | $0 (mocked) |

**Pricing table (FROZEN, `cost_constants.py`):** `gpt-4o` $2.50/$10.00 per 1M in/out; `gpt-4o-mini` $0.15/$0.60 per 1M.
**Per-document circuit breaker (FROZEN):** `PER_DOCUMENT_TOKEN_CEILING` default 2,000,000 cumulative tokens → `MeteredChat` raises `CostBudgetExceeded`. The orchestrator (this unit) catches it → writes `apollo_ingest_errors(stage=<current>, error_class='CostBudgetExceeded')` + flips run `status='failed'` + `fail_job`.

**CLAUDE.md model-default note (NOT a deviation):** the project CLAUDE.md QA pipeline pins `GPT-4o via MAIN_MODEL` and `text-embedding-3-large (3072 dims)`. The Apollo stages inherit `MAIN_MODEL`=`gpt-4o` / `APOLLO_CHEAP_MODEL`=`gpt-4o-mini` (3B2f) and `embed_text` (`indexing/document_embedder.py`, text-embedding-3-large). **WU-3B2g introduces NO model and NO embedding of its own**, so there is nothing to reject against the defaults — the orchestrator only injects the already-configured frozen callables.

**Monthly projected cost (this unit, per-PR / CI):** $0 (all LLM calls mocked; no real tokens in CI per §8B.7 Tier-1).
**Runtime projection is out of scope** (the feature ships flag-OFF; activation + Tier-2/Tier-3 real-LLM cost lives in 3B2h's nightly/release harness, scaffolded-not-run here).

## 4. Failure paths

### 4a. Per external call (all routed through frozen `MeteredChat`)
- **Retry policy:** the JOB is the retry unit, not the call. A stage exception bubbles to the orchestrator, which fails the run; the worker's next sweep re-claims the job (lease/`attempt_count`) and re-runs the whole document idempotently (§2c). Max retries = `MAX_ATTEMPTS` (3, FROZEN `cost_constants.py`) enforced by the FROZEN `fail_job` dead-letter.
- **Fallback behavior after exhausting retries:** `fail_job` moves the job to terminal `state='failed'` (poison job, no further claim); the orchestrator has already flipped the run `status='failed'`. The document is NOT re-enqueued (the partial-unique-index no longer blocks because both are terminal, but the content-hash short-circuit only short-circuits on `succeeded` — a failed doc CAN be re-enqueued by a future re-upload, which is correct).
- **No "best-effort" without a dead-letter:** every stage call is wrapped; an unparseable/empty LLM response is already fail-CLOSED inside the frozen stages (`SolutionDraftError`, the pairing `_fail_closed_verdict`, `TagMintError`) — the orchestrator maps each to an `apollo_ingest_errors` row + a rejection or a run failure, never silently continues.

### 4b. Stage outcome → observability write (the decision table this unit owns)

| Stage outcome | Observability write | Run/Job effect |
|---|---|---|
| `find_or_generate` raises `SolutionDraftError` | `apollo_ingest_errors(stage='find_or_generate', error_class='SolutionDraftError')` | run `status='failed'` + `fail_job` (per-document terminal/retry) |
| `validate_pair` → `rejection_from_verdict` returns a `Rejection` | `apollo_rejected_problems(rejected_stage='pairing_gate', failed_gate=NULL, diagnostic=rej.diagnostic, payload=...)` | run CONTINUES to next candidate; `n_rejected += 1` (assign-recompute) |
| `tag_and_mint` raises `TagMintError` | `apollo_ingest_errors(stage='tag_mint', error_class='TagMintError')` | run `status='failed'` + `fail_job` |
| `run_promotion_lint` → `PromotionResult(ok=False, failed_gate=g)` | `apollo_rejected_problems(rejected_stage='promotion_lint', failed_gate=g, diagnostic=res.diagnostic)` | run CONTINUES; `n_rejected += 1`; problem stays Tier-1 |
| `run_promotion_lint` → `ok=True` | (none) | `n_promoted += 1`; tier 1→2 + `:Canon` rebuild |
| `MeteredChat` raises `CostBudgetExceeded` (any stage) | `apollo_ingest_errors(stage=<current>, error_class='CostBudgetExceeded', context={tokens, ceiling})` | run `status='failed'` + `fail_job` (a cost abort is terminal-for-this-attempt) |
| `project_canon` raises `CanonProjectionError` | `apollo_ingest_errors(stage='promotion', error_class='CanonProjectionError')` | run `status='failed'` + `fail_job` (the Postgres tier-flip already committed is idempotently re-projected on retry) |
| stage error survived (any caught non-terminal) | `apollo_ingest_errors` row | per the cell above |

**Distinction (binding):** a per-CANDIDATE rejection (`Rejection` / lint fail) does NOT fail the run — it is logged and the run continues to the next candidate, then ends `status='succeeded'`. A per-DOCUMENT error (`SolutionDraftError`, `TagMintError`, `CostBudgetExceeded`, `CanonProjectionError`, any unexpected exception) fails the WHOLE run. This is the §8B "rejected ≠ errored" line.

### 4c. DLQ / terminal-status reconciliation / lease-reaper (§9 OPS-5)
- **DLQ:** `apollo_provisioning_jobs.state='failed'` (poison jobs) + `apollo_ingest_errors` (per-error audit) + `apollo_rejected_problems` (per-candidate rejections). All course-scoped, human-inspectable.
- **The wedge this prevents:** a worker that dies mid-document leaves the run `status='running'` and the job `running`; the FROZEN claim re-claims on lease expiry, but if the worker died AFTER the lease-renew window and before any terminal write, the run row is stuck `'running'`. The **lease-reaper** pass (each worker sweep, BEFORE claiming) finds any `apollo_provisioning_jobs` row with `state='running' AND lease_expires_at < now()`, re-opens it (delegates to the FROZEN claim's re-claim predicate) AND marks its linked `ingest_run.status='failed'` so the run never stays `'running'`. A reaped job is then re-claimable; its document re-runs idempotently.
- **Observability / alert threshold:** ONE structured `_LOG.info("apollo_provision_sweep", extra={run_id, status, n_promoted, n_rejected, llm_cost})` per sweep (mirrors the janitor's single-line-per-sweep), plus the frozen `provisioning_claim`/`provisioning_fail` lines. Alert signal for ops: a rising `apollo_provisioning_jobs` count in `state='failed'`, or `apollo_ingest_errors` rows with `error_class='CostBudgetExceeded'`.

## 5. Security check

- **API keys from env only.** WU-3B2g builds NO OpenAI client. It injects the FROZEN `MeteredChat` (which reads `OPENAI_API_KEY` via the SDK, never an argument/log) and the FROZEN `embed_text`. No key touches any new file.
- **No secrets in DB rows or logs.** The observability rows (`ingest_runs`/`ingest_errors`/`rejected_problems`) carry counts, ids, gate numbers, diagnostics, and `context` JSON — NEVER a key, never raw LLM prompt/completion bodies, never PII. The orchestrator's `_LOG.info` per-sweep line carries `run_id`/`status`/counts/`llm_cost` only (mirrors the frozen `pairing_verdict`/`llm_call` log convention which already scrubs bodies).
- **No PII in embedding inputs.** The only embedding is the FROZEN dedup ladder embedding the entity `scope_summary` (academic concept text, authored from `display_name`+symbols). WU-3B2g passes course-material-derived problems only; it reads NO learner state and never sees student PII. Retention: `scope_summary` is non-PII curriculum metadata; no deletion policy beyond the existing `ON DELETE CASCADE` on `search_space_id`.
- **Service-role key:** the worker runs server-side (Railway process), uses the app's async engine via `get_async_session`; no client-reachable surface is added. The enqueue runs inside the existing teacher-upload worker session (already server-side).
- **RLS awareness:** the 5 new tables are RLS-stopgapped (ENABLE, zero policies — default-deny to PostgREST, migration 030 frozen). The worker accesses them via the service connection, not PostgREST, so the stopgap does not block it. No new table read bypasses RLS.
- **Flag-OFF default = the headline safety.** `APOLLO_AUTOPROVISION_ENABLED` default OFF everywhere (incl. prod/staging) + the process scaled to 0 replicas means no auto-provisioned content reaches a student without an explicit human calibration/deploy step (§8B.3 / OPS-6).

No security rule is violated. PASS.

## 6. Prior art in repo (mirror, do not invent)

1. **Worker SHELL — `apollo/learner_janitor_worker.py` (FROZEN, mirror EXACTLY).** The canonical dormant-worker template: `_int_env` (imported from `apollo.handlers.learner_janitor`, do NOT redefine), default-OFF flag read per-iteration (`_janitor_enabled`), `_run_one_iteration` (the body — unit-testable, NOT under the loop pragma), `_loop` (`while not stop_event.is_set():  # pragma: no cover - loop shell`), `_install_signal_handlers` (try/except for Windows `NotImplementedError`), `main()` building ONE resource on the running loop with `await neo.close()` in `finally`, single `asyncio.run(main())` under `if __name__ == "__main__":  # pragma: no cover`. WU-3B2g's `provision_worker.py` copies this structure 1:1, swapping the drain body for claim+run_provisioning+lease-reaper.
2. **Claim/lease DRAIN — `apollo/provisioning/queue.py` (FROZEN, 3B2f).** `claim_provisioning_job(session, *, lease_owner, lease_seconds) -> ClaimedJob | None`, `complete_job`, `fail_job(... ) -> state`, `release_job`. The drain is NOT redefined here (§9 FEAS-1) — the worker calls it.
3. **Real-PG committed-engine + migration-chain harness — `tests/database/test_apollo_provisioning_queue.py` (3B2f).** The exact pattern for `tests/database/test_apollo_provisioning_orchestration.py`: `_STUB_DDL` (auth.users + aita_search_spaces), the content-scoped `_TOUCHES_TARGETS` regex + `_EXCLUDE_FROM_CHAIN` selecting chain `[009,018,025,026]→030`, `_create_db`/`_apply_chain`, a session-scoped `_queue_dsns`, a function-scoped `committed_engine` that DELETEs the provisioning tables after each test, and `asyncpg`-COMMITTED seed helpers. Reuse VERBATIM (the partial-unique-index + cross-connection visibility require committed rows, which the savepoint `db_session` cannot provide).
4. **Savepoint `db_session` fixture — `tests/conftest.py:163` (FROZEN).** A NullPool real-pgvector session with `create_savepoint` join mode, schema built by `create_all`. Used for the orchestrator STAGE-sequencing unit tests that don't need cross-connection visibility (mock `neo`, mock `metered_chat`).
5. **Enqueue/idempotency seam — `knowledge/teacher_weekly.py:1131-1174` finalize block + `:1041` `compute_content_hash` + `:1102` `indexed_doc = session.get(AITADocument, document_id)`.** The thin enqueue rides this committed session; `content_hash` is read off the finalized `AITADocument` row (column `database/models.py:135`).
6. **Promotion-lint + Canon — `apollo/provisioning/promotion_lint.py::run_promotion_lint(graph, *, canonical_symbols, normalization_map, existing_problem_hashes)` + `apollo/knowledge_graph/canon_projection.py::project_canon(db, neo, *, search_space_id, concept_id)` (both FROZEN).** `promote.py` calls them; it never re-implements gate logic or MERGE.
7. **Seed annotation — `apollo/persistence/learner_model_seed.py::annotate_reference_solution(problem, key_for_node) -> dict` + `validate_reference_graph` (FROZEN).** `promote.py` builds the `graph` dict fed to `run_promotion_lint` by annotating the minted reference solution with `entity_key` + `declared_paths` (the lint requires the ANNOTATED dict per its docstring). This is the §8 seed's promotion-time annotation, reused — NOT re-authored.

**First pipeline of its kind?** No — this is the SAME async Postgres-queue/worker pattern the teacher-upload pipeline and the learner-janitor already use. No Supabase Edge Function / pgmq / pg_cron is involved (the project is FastAPI + Railway Procfile workers, NOT Supabase Edge Functions — the ai-pipeline domain's Edge-Function vocabulary maps here to "Procfile worker process + Postgres SKIP-LOCKED queue").

## 7. Structural prep (neighborhood scan)

Scanned the change-path artifacts (the files this unit creates/edits + their one-ring dependents):

- **`knowledge/teacher_weekly.py`** — large module (~1200+ lines) but the edit is a THIN ~8-line enqueue at one seam inside an existing finalize block; it adds ONE import (`from apollo.provisioning.enqueue import enqueue_provisioning_job`) and one call. CBO impact +1 import; WMC impact negligible (no new responsibility in the function — it delegates). **No prep needed** — do NOT refactor this file; keeping the edit thin is the contract (owner-doc boundary). Flag: the module is already >800 lines (over the project soft cap), but it is FROZEN-adjacent and out of this unit's refactor scope.
- **`apollo/provisioning/orchestrator.py` (NEW)** — must stay <~250 lines / one responsibility (sequence stages, write observability, reconcile terminal status). The per-stage error→observability mapping is a single private dispatch (`_record_stage_error`) to avoid retry/error-handling sprawl (one shared pattern, not ad-hoc per stage). Promote logic is EXTRACTED into `promote.py` so the orchestrator does not also own gate→reject mapping. **Compliant by construction.**
- **`apollo/provision_worker.py` (NEW)** — mirrors the 147-line janitor worker; same size class. The lease-reaper is a small private coroutine (`_reap_expired`) called from `_run_one_iteration`. **Compliant.**
- **`apollo/provisioning/__init__.py`** — re-exports grow by 4 names (`enqueue_provisioning_job`, `run_provisioning`, `promote`, and any new error types). Still a flat re-export list, no logic. **Compliant.**
- **Pipeline coupling check:** the stages share state ONLY through the typed handoffs (`CandidateQuestion`, `ReferenceSolutionDraft`, `PairingVerdict`, `ApprovedPair`, `MintPlan`, `PromotionResult`) and the `apollo_*` rows — no stage reads another stage's internals. The orchestrator is the single owner of the `ingest_run`/job lifecycle. **No shared-mutable-state debt introduced.**

**Verdict: neighborhood is clean.** No structural-prep steps precede the feature work. (Budget check: 0% of plan steps are prep — well under the 30% ceiling.)
- Verify: `wc -l apollo/provisioning/orchestrator.py apollo/provisioning/promote.py apollo/provision_worker.py apollo/provisioning/enqueue.py` each under ~250 lines after implementation.

## 8. Public signatures (exact)

All new code is `from __future__ import annotations`, type-annotated, immutable-style (return new objects; the ONE intended ORM mutation is row attribute assignment, flushed by the caller's session — mirroring the frozen `tag_mint_persist`/`metered_chat` convention).

### 8a. `apollo/provisioning/enqueue.py` (NEW)
```python
async def enqueue_provisioning_job(
    session: AsyncSession,
    *,
    search_space_id: int,
    document_id: int,
    content_hash: str | None,
) -> int | None:
    """Idempotent enqueue at the indexing-completion seam. Returns the new
    apollo_provisioning_jobs.id, or None when short-circuited (an unchanged
    re-upload already has a status='succeeded' run for (document_id,
    content_hash), OR a partial-unique-index collision means an open job
    already exists). Runs INSIDE the caller's (teacher_weekly) committed session
    — does NOT commit/rollback itself (the finalize block owns the commit)."""
```
- Short-circuit: SELECT `apollo_ingest_runs.id` WHERE `document_id == document_id AND content_hash == content_hash AND status == 'succeeded'`; if found → return None.
- Else: create an `apollo_ingest_runs(search_space_id, document_id, content_hash, status='queued')` row (`flush` to get its id) + an `apollo_provisioning_jobs(search_space_id, document_id, state='pending', ingest_run_id=<run.id>)` row. `flush`. On `IntegrityError` from the partial-unique-index (an open job already exists), roll back the SAVEPOINT for just this insert (use a nested `begin_nested()` around the job insert) and return None — never crash the upload commit.
- **Boundary note:** `content_hash` may be None on legacy rows; a None hash is enqueued (no short-circuit possible), which is safe (the job runs once; a later real hash short-circuits).

### 8b. `apollo/provisioning/orchestrator.py` (NEW)
```python
class ProvisioningOutcome(BaseModel):   # frozen-ish DTO (model_config frozen)
    run_id: int
    status: str                # 'succeeded' | 'failed'
    n_questions_scraped: int
    n_promoted: int
    n_rejected: int
    n_dedup_merged: int

async def run_provisioning(
    db: AsyncSession,
    neo,                       # Neo4jClient | a duck-typed mock (project_canon target)
    *,
    job: ClaimedJob,           # from queue.claim_provisioning_job (3B2f)
    metered_chat: MeteredChat, # the cost-aggregating chat client (3B2f)
    embed_fn: Callable[[str], Sequence[float]] | None = None,   # default indexing.document_embedder.embed_text
    retrieve_fn: Callable[..., Awaitable[Sequence[GroundingSpan]]] | None = None,  # default a course-corpus adapter
) -> ProvisioningOutcome:
    """Run the 6 stages for one document. Loads the run row by
    job.ingest_run_id, sets status='running'+started_at, drives:
      1. scrape_questions + write_tier1_problems (3B2d)
      2. find_or_generate per candidate (3B2e)
      3. validate_pair + rejection_from_verdict (3B2e)
      4. build_approved_pair + tag_and_mint (3B2e->3B2d)
      5. (dedup resolve_candidate runs INSIDE tag_and_mint, 3B2c)
      6. promote (run_promotion_lint + project_canon, promote.py)
    Per-candidate Rejection/lint-fail -> apollo_rejected_problems + continue.
    Per-document error -> apollo_ingest_errors + status='failed' (NEVER left
    'running'). Aggregates n_* onto the run row (assign, not +=, so a replay
    does not inflate). Returns the outcome; the WORKER calls complete_job/fail_job
    based on outcome.status."""
```
- The orchestrator OWNS the session transaction shape: it `commit`s the run/observability writes; the chat metering accrues on the run row in the same session. It does NOT call `complete_job`/`fail_job` (that is the worker's terminal decision, so the test can drive `run_provisioning` in isolation).
- `metered_chat.main` (find_or_generate) and `metered_chat.cheap` (validate_pair judge) are injected DIRECTLY because those stages call their callables WITH keywords (`chat_fn(purpose=…, messages=…, …)`). **scrape and tag_and_mint are the two positional-string seams** and need an adapter: scrape uses `metered_chat.scrape_chat_fn(system_prompt)` (frozen), and **tag_and_mint must be handed a positional-string adapter `lambda s: metered_chat.cheap(purpose="tag_mint", messages=[{"role":"user","content":s}])`** because `tag_mint._parse_tag` invokes its `chat_fn(json.dumps(problem))` POSITIONALLY while `MeteredChat.cheap` is keyword-only (`def cheap(self, *, purpose, messages, …)`). Handing `.cheap` directly to `tag_and_mint` would raise `TypeError` at runtime and promote nothing. (Correction to the earlier "shapes match" claim: `.cheap`/`.main` match the find_or_generate/validate_pair keyword shape, NOT the tag_and_mint positional-string shape.)
- The orchestrator computes gate-8's concept-scoped `existing_problem_hashes` (load tier=2 payloads for `mint_plan.concept_id` → `problem_dup_hash`) before `promote`, and `promote` RE-HOMES the promoted row to `mint_plan.concept_id` and stores the COMPLETE annotated problem as the payload (both the student selector and gate-8 validate the payload as a `Problem`).
- Private helpers (all `# unit-tested via run_provisioning`, none pragma'd): `_record_stage_error(db, *, run, stage, exc)`, `_record_rejection(db, *, run, rejected_stage, failed_gate, diagnostic, payload, concept_id)`, `_recompute_counts(run, *, scraped, promoted, rejected, merged)`.

### 8c. `apollo/provisioning/promote.py` (NEW)
```python
async def promote(
    db: AsyncSession,
    neo,
    *,
    problem: dict,                 # the Problem-validatable dict (from the ApprovedPair)
    mint_plan,                     # MintPlan (3B2d) — concept_id + minted/merged entity ids
    search_space_id: int,
    concept_problem_id: int,       # the apollo_concept_problems.id to flip 1->2
    existing_problem_hashes: set[str],
) -> "PromoteResult":
    """Annotate the minted reference graph (annotate_reference_solution, frozen)
    -> run_promotion_lint(graph, canonical_symbols=<concept's authored set>,
    normalization_map=<concept's map>, existing_problem_hashes=...). On PASS:
    store reference_solution + entity_key/declared_paths into the
    apollo_concept_problems.payload, flip tier=1->2 + solution_source, flush,
    then project_canon(db, neo, search_space_id=..., concept_id=mint_plan.concept_id).
    On FAIL: return a PromoteResult(promoted=False, failed_gate, diagnostic) so the
    orchestrator writes the apollo_rejected_problems row (promote does NOT write it
    — single rejection-write owner is the orchestrator)."""

class PromoteResult(BaseModel):
    promoted: bool
    failed_gate: int | None
    diagnostic: str
```
- `canonical_symbols`/`normalization_map` are READ from the concept row (`apollo_concepts.canonical_symbols['symbols']` / `normalization_map`) authored by `tag_and_mint` (3B2d). Gate 4 is non-vacuous because of that authoring.
- `promote` is idempotent: a re-run flips an already-`tier=2` row to `tier=2` (no-op) and re-MERGEs `:Canon` (idempotent). It keys on `concept_problem_id`, never an insert.

### 8d. `apollo/provision_worker.py` (NEW) — mirror `learner_janitor_worker.py`
```python
_AUTOPROVISION_ENABLED_FLAG = "APOLLO_AUTOPROVISION_ENABLED"   # default OFF
SWEEP_LIMIT = _int_env("APOLLO_PROVISION_SWEEP_LIMIT", 1)
POLL_SECONDS = _int_env("APOLLO_PROVISION_POLL_SECONDS", 60)
LEASE_SECONDS = _int_env("APOLLO_PROVISION_LEASE_SECONDS", 1800)
LEASE_OWNER = ...   # f"provision-{socket.gethostname()}-{os.getpid()}" or similar stable id

def _autoprovision_enabled() -> bool: ...   # per-iteration env read, default OFF
async def _reap_expired(session_factory) -> int:
    """Lease-reaper: each expired 'running' job -> mark its ingest_run failed
    (status='failed') so it never stays 'running'; the FROZEN claim predicate
    re-claims the job itself. Returns the count reaped. (§9 OPS-5)"""
async def _drain_one(neo, *, session_factory, metered_chat_factory) -> ProvisioningOutcome | None:
    """Claim (3B2f) -> run_provisioning -> complete_job|fail_job. None when
    nothing claimable. The MeteredChat is built per-job bound to the claimed
    run row."""
async def _run_one_iteration(neo, *, stop_event) -> None:
    """flag-read -> (reap + drain | skip) -> sleep. Wrapped in try/except so one
    bad sweep logs + survives. ONE _LOG.info('apollo_provision_sweep', extra=
    {run_id,status,n_promoted,n_rejected,llm_cost}) per drained job."""
async def _loop(neo, *, stop_event) -> None: ...    # while-shell, pragma: no cover
def _install_signal_handlers(loop, stop_event) -> None: ...   # try/except (Windows)
async def main() -> None: ...    # one Neo4jClient.from_env(), finally await neo.close()
if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
```
- `_int_env` is IMPORTED from `apollo.handlers.learner_janitor` (REUSE the frozen helper, do NOT redefine — mirrors the janitor worker's import).
- Coverage posture: the bare `while` loop shell + the `asyncio.run` entry are `# pragma: no cover` ONLY because `_run_one_iteration` + `_reap_expired` + `_drain_one` + `_autoprovision_enabled` + `_install_signal_handlers` + `main` all carry real tests (the §11 worker tests + the real-PG reaper test). This MIRRORS the janitor worker's coverage posture exactly.

### 8e. `apollo/provisioning/__init__.py` (EDIT)
Add re-exports: `enqueue_provisioning_job`, `run_provisioning`, `ProvisioningOutcome`, `promote`, `PromoteResult`. (Append to the existing flat list; do not reorder.)

### 8f. `knowledge/teacher_weekly.py` (EDIT, THIN ~8 lines)
Inside the finalize block, AFTER `upload.status = UPLOAD_STATUS_READY` and BEFORE `await session.commit()` (so the enqueue rides the SAME commit, `:1131-1174`):
```python
# WU-3B2g §8B: enqueue Apollo auto-provisioning for this finished document.
# Idempotent (content-hash short-circuit + partial-unique-index); a no-op when
# APOLLO_AUTOPROVISION_ENABLED is OFF nothing DRAINS the job, so upload behavior
# is byte-identical to today.
from apollo.provisioning.enqueue import enqueue_provisioning_job  # local import: avoid apollo import at module load
await enqueue_provisioning_job(
    session,
    search_space_id=int(upload.search_space_id),
    document_id=document_id,
    content_hash=getattr(indexed_doc, "content_hash", None),
)
```
- Local import (not top-of-module) keeps `apollo` off the teacher_weekly import path at load and matches the repo's deferred-import pattern at this seam (`from indexing.checkpoint_indexer import ...` is already local at `:1024`).

### 8g. `Procfile` (EDIT) — add a 4th line
```
apollo-provision: python -m apollo.provision_worker
```
(after the existing `web` / `worker` / `apollo-janitor` lines).

## 9. Files to create / edit

ALL changes stay inside the scope allowlist. NO new package, NO migration (uses 030's frozen tables).

**Create:**
- `apollo/provisioning/enqueue.py` — `enqueue_provisioning_job`.
- `apollo/provisioning/orchestrator.py` — `run_provisioning` + `ProvisioningOutcome` + private helpers.
- `apollo/provisioning/promote.py` — `promote` + `PromoteResult`.
- `apollo/provision_worker.py` — the dormant worker SHELL + lease-reaper.
- `apollo/provisioning/tests/test_enqueue.py` — enqueue idempotency unit tests (savepoint `db_session`).
- `apollo/provisioning/tests/test_orchestrator.py` — stage-sequencing + error-mapping unit tests (mock neo/metered_chat/stages).
- `apollo/provisioning/tests/test_promote.py` — promote pass/fail + idempotency unit tests (mock neo).
- `apollo/tests/test_provision_worker.py` — worker-shell unit tests (mirror `apollo/tests/test_learner_janitor_worker.py`; mock claim/run/sleep/signals). *(In-scope: `apollo/provision_worker.py` is the scoped file; its test belongs beside the janitor worker test, consistent with the frozen layout. If the harness restricts test files to the enumerated set, fold these worker-shell tests into `tests/database/test_apollo_provisioning_orchestration.py` as pure-unit cases — see §11 note.)*
- `tests/database/test_apollo_provisioning_orchestration.py` — real-PG: enqueue+short-circuit, observability writes, lease-reaper, terminal-status reconciliation, flag-OFF non-regression, the 3 mutation proofs.

**Edit:**
- `knowledge/teacher_weekly.py` — THIN ~8-line enqueue at the finalize seam.
- `apollo/provisioning/__init__.py` — re-export the new public names.
- `Procfile` — add `apollo-provision:` line.

**Owner docs (same commit, §13):** `docs/architecture/apollo.md`, `docs/architecture/domain-data.md`, `docs/architecture/indexing.md` (+ note the `_overview.md` Procfile-process boundary, §13).

> **Scope note on the worker test file:** the binding scope list enumerates the four test files `test_enqueue.py`, `test_orchestrator.py`, `test_promote.py`, `tests/database/test_apollo_provisioning_orchestration.py`. The worker-shell unit tests (`apollo/tests/test_provision_worker.py`) target `apollo/provision_worker.py` (a scoped SOURCE file). Two compliant options, executor picks: **(A)** add `apollo/tests/test_provision_worker.py` (mirrors the frozen janitor layout — RECOMMENDED, keeps the worker's pure-unit coverage where reviewers expect it); **(B)** if the allowlist is strict-set, place the worker-shell pure-unit cases inside `tests/database/test_apollo_provisioning_orchestration.py` (already in-scope) guarded so the pure-unit ones need no container. Either way the worker's `_run_one_iteration`/`_reap_expired`/`_drain_one`/flag-read carry real tests (the pragma posture in §8d depends on it).

## 10. TDD-ordered step-by-step

RED first every step (write the test, watch it fail, then implement to GREEN). No skip/xfail/assert-nothing. Mocks deterministic, no live API.

- [ ] **Step 1 — enqueue (RED→GREEN).**
  - Test file: `apollo/provisioning/tests/test_enqueue.py` (savepoint `db_session`; `create_all` schema — note the partial-unique-index is migration-only, so the open-job-collision case is proved in the real-PG file, §11; this file proves the SELECT short-circuit + the happy insert).
  - Write tests T-EQ1..T-EQ4 (§11). Run — RED (no module).
  - Implement `apollo/provisioning/enqueue.py` per §8a. GREEN.
  - Verify: `pytest apollo/provisioning/tests/test_enqueue.py -q`.

- [ ] **Step 2 — promote (RED→GREEN).**
  - Test file: `apollo/provisioning/tests/test_promote.py` (savepoint `db_session`; mock `neo` so `project_canon` is a no-op AsyncMock or a stub Neo4jClient; seed a concept with authored `canonical_symbols` + an `apollo_concept_problems` Tier-1 row + a minted reference graph).
  - Write T-PR1..T-PR6 (§11). RED.
  - Implement `apollo/provisioning/promote.py` per §8c (annotate → `run_promotion_lint` → tier flip + payload + `project_canon`; FAIL → `PromoteResult(promoted=False, ...)`). GREEN.
  - Verify: `pytest apollo/provisioning/tests/test_promote.py -q`.

- [ ] **Step 3 — orchestrator (RED→GREEN).**
  - Test file: `apollo/provisioning/tests/test_orchestrator.py` (savepoint `db_session`; mock `metered_chat` with canned `cheap`/`main`/`scrape_chat_fn`; mock `retrieve_fn`/`embed_fn`; mock `neo`). Seed a `ClaimedJob` + an `apollo_ingest_runs` row + chunks.
  - Write T-OR1..T-OR9 (§11). RED.
  - Implement `apollo/provisioning/orchestrator.py` per §8b: load run → status running → scrape+write_tier1 → per-candidate (find_or_generate → validate_pair → build_approved_pair → tag_and_mint → promote) with the §4b decision table → recompute counts (assign) → status succeeded/failed. GREEN.
  - Verify: `pytest apollo/provisioning/tests/test_orchestrator.py -q`.

- [ ] **Step 4 — worker shell + lease-reaper (RED→GREEN).**
  - Test file: `apollo/tests/test_provision_worker.py` (pure-unit; mock `claim_provisioning_job`/`run_provisioning`/`complete_job`/`fail_job`/`asyncio.sleep`/signals — mirror `test_learner_janitor_worker.py`).
  - Write T-WK1..T-WK8 (§11). RED.
  - Implement `apollo/provision_worker.py` per §8d (copy the janitor structure; swap the body for reap+claim+run_provisioning+terminal-decision). GREEN.
  - Verify: `pytest apollo/tests/test_provision_worker.py -q`.

- [ ] **Step 5 — `__init__.py` re-exports + `Procfile` line (GREEN, trivial).**
  - Add the 5 re-exports to `apollo/provisioning/__init__.py`; add the `apollo-provision:` Procfile line.
  - Covered by an import test (T-OR1 imports through the package surface) + a Procfile assertion (T-DB7).

- [ ] **Step 6 — thin enqueue wire-in (RED→GREEN, real-PG).**
  - Test file: `tests/database/test_apollo_provisioning_orchestration.py` (committed-engine + migration-chain harness, §6 prior art 3).
  - Write the real-PG tests T-DB1..T-DB9 (§11) — RED for the wire-in cases first.
  - Edit `knowledge/teacher_weekly.py` per §8f (THIN enqueue inside the finalize commit). GREEN.
  - **Non-regression guard:** confirm the existing teacher-upload tests stay green (`pytest tests/database/test_teacher_upload_checkpoint_postgres.py -q`). With the flag OFF and no worker, the enqueue adds a `pending` job + a `queued` run but changes NO upload-facing column — prove the upload READY/COMPLETED path byte-identical (T-DB6).

- [ ] **Step 7 — full real-PG suite + mutation proofs (GREEN, the load-bearing assertions).**
  - Finish T-DB1..T-DB9 incl. the 3 mutation proofs (§11 mutation table). Run the real-PG file green-NOT-skipped (Docker up, `.venv/Scripts/python.exe`).

- [ ] **Step 8 — owner-doc reconciliation (same commit, §13).**
  - Edit `apollo.md`, `domain-data.md`, `indexing.md`; bump `last_verified` to 2026-06-20; note the `_overview.md` Procfile-process boundary (§13).

- [ ] **Step 9 — coverage gate.**
  - `pytest --cov=. --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2f-queue-metered --fail-under=95`. Fix any uncovered changed line (the only allowed pragmas are the worker `while`-shell + `asyncio.run` entry, §8d).

## 11. Full test list

Mocking conventions: LLM = a Python callable returning a canned JSON string (no network); embeddings = a deterministic vector function; `neo` = `AsyncMock`/stub so `project_canon` is a no-op observable call; `metered_chat` = a fake exposing `.cheap`/`.main`/`.scrape_chat_fn(sys)` returning canned strings and optionally raising `CostBudgetExceeded`. Real-PG file uses the committed-engine harness; pure-unit files use the savepoint `db_session` or no DB.

### `apollo/provisioning/tests/test_enqueue.py` (savepoint `db_session`)
- **T-EQ1 `test_enqueue_inserts_run_and_job`** — given a clean DB, `enqueue_provisioning_job(session, search_space_id, document_id=7, content_hash="h")` inserts one `apollo_ingest_runs(status='queued')` + one `apollo_provisioning_jobs(state='pending', ingest_run_id=<run>)`; returns the job id. Asserts both rows + the FK link.
- **T-EQ2 `test_enqueue_short_circuits_on_succeeded_run`** — pre-seed an `apollo_ingest_runs(document_id=7, content_hash="h", status='succeeded')`; the call returns None and inserts NO new job/run (count unchanged). The MUTATION-DISCRIMINATING assert for §2a (removing the SELECT → a job appears → REDs).
- **T-EQ3 `test_enqueue_does_not_short_circuit_on_failed_or_different_hash`** — a prior run with `status='failed'` OR a DIFFERENT `content_hash` does NOT short-circuit (a changed re-upload re-enqueues); returns a new job id. (Guards the short-circuit is `succeeded AND same-hash` only.)
- **T-EQ4 `test_enqueue_none_content_hash_enqueues`** — `content_hash=None` (legacy doc) cannot short-circuit; inserts a job. (Boundary.)

### `apollo/provisioning/tests/test_promote.py` (savepoint `db_session`, mock `neo`)
- **T-PR1 `test_promote_pass_flips_tier_and_payload`** — seed a concept with authored `canonical_symbols`/`normalization_map`, a Tier-1 `apollo_concept_problems` row, and a mintable reference graph; `promote(...)` with a graph that passes all 8 gates → `PromoteResult(promoted=True)`, the row is `tier=2`, `payload.reference_solution` present (annotated with `entity_key`/`declared_paths`), `solution_source` set. Asserts `project_canon` was AWAITED once with `concept_id=mint_plan.concept_id`.
- **T-PR2 `test_promote_fail_returns_failed_gate_no_flip`** — a graph that fails gate 4 (foreign symbol) → `PromoteResult(promoted=False, failed_gate=4)`, the row STAYS `tier=1`, `project_canon` NOT called. (Proves promote never promotes a lint failure; the §4b rejection-write is the orchestrator's job, asserted there.)
- **T-PR3 `test_promote_reads_concept_authored_symbols`** — gate 4 passes ONLY because the concept's authored `canonical_symbols` cover the problem's symbols; with the concept's symbols EMPTIED, the same graph fails gate 4. (Proves promote reads the authored set, not a vacuous one.)
- **T-PR4 `test_promote_passes_existing_hashes_to_gate8`** — pre-seed the problem's dup-hash into `existing_problem_hashes` → gate 8 fails (`failed_gate=8`). (Proves the dup-hash plumbing.)
- **T-PR5 `test_promote_idempotent_rerun`** — call `promote` twice on the same passing problem → second call no-ops the tier flip (already 2), re-MERGEs `:Canon` (project_canon called again, idempotent), `PromoteResult(promoted=True)` both times. (§2c partial-progress.)
- **T-PR6 `test_promote_canon_error_propagates`** — `project_canon` raises `CanonProjectionError` (mock side_effect) → `promote` re-raises (the orchestrator maps it to an `apollo_ingest_errors` row + failed run). Asserts the tier flip already flushed is NOT rolled back inside promote (the caller's session owns rollback).

### `apollo/provisioning/tests/test_orchestrator.py` (savepoint `db_session`; mock metered_chat/retrieve/embed/neo)
- **T-OR1 `test_run_provisioning_happy_path_promotes`** — chunks → scrape yields 1 candidate → find_or_generate (generated) → validate_pair approves → tag_and_mint mints → promote passes. Asserts: run `status='succeeded'`, `n_questions_scraped>=1`, `n_promoted==1`, `n_rejected==0`, the Tier-1 row flipped to Tier-2. (Imports `run_provisioning` through `apollo.provisioning` package surface — covers the __init__ re-export.)
- **T-OR2 `test_run_provisioning_pairing_rejection_continues`** — `validate_pair` → `rejection_from_verdict` returns a `not_paired` `Rejection` → one `apollo_rejected_problems(rejected_stage='pairing_gate', failed_gate IS NULL)` row, the run STILL ends `status='succeeded'`, `n_rejected==1`, `n_promoted==0`. (§4b: a rejection ≠ a run failure.)
- **T-OR3 `test_run_provisioning_lint_rejection_continues`** — promote returns `PromoteResult(promoted=False, failed_gate=4)` → one `apollo_rejected_problems(rejected_stage='promotion_lint', failed_gate=4)` row, run `succeeded`, `n_rejected==1`, problem stays Tier-1.
- **T-OR4 `test_run_provisioning_solution_error_fails_run`** — `find_or_generate` raises `SolutionDraftError` → one `apollo_ingest_errors(stage='find_or_generate', error_class='SolutionDraftError')` row + run `status='failed'`. (Per-document terminal error.)
- **T-OR5 `test_run_provisioning_tagmint_error_fails_run`** — `tag_and_mint` raises `TagMintError` → `apollo_ingest_errors(stage='tag_mint')` + run failed.
- **T-OR6 `test_run_provisioning_cost_abort_fails_run`** — `metered_chat` raises `CostBudgetExceeded` mid-stage → `apollo_ingest_errors(stage=<current>, error_class='CostBudgetExceeded', context has tokens/ceiling)` + run failed. (The cost-ceiling abort path.)
- **T-OR7 `test_run_provisioning_recomputes_counts_on_replay`** — run the SAME job twice (scrape no-ops the second time via the frozen SELECT-skip); assert the counts are RE-ASSIGNED not doubled (`n_questions_scraped`/`n_promoted` identical across both runs). (§2c / mutation: using `+=` would double → REDs.)
- **T-OR8 `test_run_provisioning_sets_started_and_finished`** — the run row gets `started_at` on entry and `finished_at` on exit (both branches), `status` transitions queued→running→(succeeded|failed). (Proves the status lifecycle never strands at `running` on the success path.)
- **T-OR9 `test_run_provisioning_empty_scrape_succeeds`** — zero candidates scraped → run `status='succeeded'`, all counts 0, no error/reject rows. (Empty-document edge.)

### `apollo/tests/test_provision_worker.py` (pure-unit; mirror janitor worker test)
- **T-WK1 `test_iteration_skips_when_flag_off`** — `APOLLO_AUTOPROVISION_ENABLED` unset → `_run_one_iteration` calls NEITHER `_reap_expired` NOR `claim_provisioning_job`; `asyncio.sleep` (patched AsyncMock) still awaited. (Flag-OFF = NO drain — the load-bearing non-regression at the worker layer.)
- **T-WK2 `test_iteration_drains_when_flag_on`** — flag on, `claim` (AsyncMock) returns a `ClaimedJob`, `run_provisioning` (AsyncMock) returns a succeeded outcome → `complete_job` awaited; one `apollo_provision_sweep` log line with run_id/status/counts/llm_cost.
- **T-WK3 `test_iteration_fails_job_on_failed_outcome`** — `run_provisioning` returns `status='failed'` → `fail_job` awaited (not `complete_job`).
- **T-WK4 `test_iteration_reaps_before_claim`** — flag on → `_reap_expired` is awaited BEFORE `claim_provisioning_job` (assert call order via a shared mock manager). (§9 OPS-5 ordering.)
- **T-WK5 `test_iteration_survives_drain_exception`** — `run_provisioning` raises a generic Exception → the iteration logs `apollo_provision_sweep_failed` and STILL awaits `asyncio.sleep` (one bad sweep does not kill the worker; `KeyboardInterrupt`/`SystemExit` still propagate — assert a raised `SystemExit` is NOT swallowed).
- **T-WK6 `test_autoprovision_enabled_reads_env_each_call`** — toggling the env between two `_autoprovision_enabled()` calls flips the return (per-iteration read, no cached flag).
- **T-WK7 `test_install_signal_handlers_registers_sigint_sigterm`** — a fake loop records `add_signal_handler(SIGINT/SIGTERM, stop_event.set)`; an `add_signal_handler` raising `NotImplementedError` (Windows) is caught and warns, not crashes.
- **T-WK8 `test_main_closes_neo_in_finally`** — `main()` with a patched `Neo4jClient.from_env` (returns a mock) + `_loop` patched to return immediately → `neo.close()` awaited even when `_loop` raises (finally).

### `tests/database/test_apollo_provisioning_orchestration.py` (real-PG committed-engine + migration-chain; GREEN-NOT-SKIPPED with Docker up)
- **T-DB1 `test_enqueue_creates_open_job_real_pg`** — committed enqueue → exactly one open `apollo_provisioning_jobs` + a `queued` run; visible to an independent connection. (Committed-visibility, not savepoint.)
- **T-DB2 `test_enqueue_partial_unique_index_collapses_duplicate_open_job`** — a SECOND `enqueue_provisioning_job` for the same `document_id` with an existing open job → returns None, still exactly ONE open job (the partial-unique-index from migration 030 enforced). MUTATION: this can only be proved on real PG (the index is migration-only).
- **T-DB3 `test_enqueue_short_circuit_unchanged_reupload`** — seed a `succeeded` run for `(document_id, content_hash)`; re-enqueue → ZERO new jobs. **MUTATION PROOF (a):** removing the content-hash short-circuit → a duplicate job appears → this test REDs.
- **T-DB4 `test_lease_reaper_reopens_expired_running_job_and_fails_run`** — seed a `running` job with `lease_expires_at < now()` linked to a `running` ingest_run; run `_reap_expired` → the run is marked `status='failed'` and the job is re-claimable by the FROZEN claim (assert a subsequent `claim_provisioning_job` returns it). **MUTATION PROOF (b):** the run is NEVER left `'running'` (§9 OPS-5) — dropping the reaper's run-fail write → the run stays `running` → this test REDs.
- **T-DB5 `test_terminal_status_reconciliation_failed_run_never_running`** — drive `run_provisioning` to a per-document error (inject a stage that raises) on the committed engine → the run ends `status='failed'`, the job is `fail_job`'d (pending-or-failed per attempt_count), and NO row is left `status='running'`. (The wedge-prevention end to end.)
- **T-DB6 `test_flag_off_upload_path_byte_identical`** — run the teacher finalize seam (or a focused harness that calls the finalize block) with `APOLLO_AUTOPROVISION_ENABLED` OFF and no worker → the upload row reaches `UPLOAD_STATUS_READY` + job `COMPLETED` with the SAME column values as a baseline run WITHOUT the enqueue edit (compare the upload/job/document rows field-by-field); the only delta is the extra `pending` provisioning job + `queued` run. **The load-bearing NON-REGRESSION.** (If invoking the full finalize is too heavy, assert: enqueue runs inside the same session, the upload-facing columns are untouched by `enqueue_provisioning_job`, and an enqueue `IntegrityError` is swallowed so it can NEVER fail the upload commit — see T-DB8.)
- **T-DB7 `test_procfile_has_apollo_provision_line`** — read `Procfile`; assert it contains `apollo-provision: python -m apollo.provision_worker` (and still the original 3 lines). (Pure file assert; lives here as the in-scope home.)
- **T-DB8 `test_enqueue_integrity_error_never_breaks_caller_commit`** — force a partial-unique-index collision inside the caller's session (pre-insert an open job on another connection, committed) → `enqueue_provisioning_job` returns None via a `begin_nested` savepoint rollback and the caller's outer session can STILL commit its other writes (assert a sentinel upload-row write in the same session survives). (Proves the enqueue can never wedge the upload.)
- **T-DB9 `test_intra_job_rerun_skips_scraped_chunks`** — run `run_provisioning` for a document twice on the committed engine → the second run inserts ZERO new Tier-1 `apollo_concept_problems` rows (the frozen `write_tier1_problems` SELECT-skip), counts re-assigned not doubled. **MUTATION PROOF (c):** the intra-job `(document_id, chunk_content_hash)` no-op — a re-run skips already-scraped chunks (§9 SPEC-1).

### Neo4j gate note
`project_canon` touches Neo4j. The plan MOCKS `neo` in all unit + real-PG tests (an `AsyncMock`/stub asserting the call shape), so NO test file lands under `apollo/knowledge_graph/` and the Neo4j Testcontainers gate is NOT triggered. (If the executor prefers a real `neo4j:5.25` Testcontainer for an end-to-end promote, that test MUST live under `apollo/knowledge_graph/` and run green — but it is NOT required by this unit; mocking is the chosen, lower-cost path and is sufficient because `project_canon` is FROZEN and already has its own WU-3C1 Neo4j tests.)

### Mutation-proof summary (binding — each must RED if the guard is removed)
| # | Property | Test | Removing what REDs it |
|---|---|---|---|
| a | content-hash short-circuit (unchanged re-upload enqueues ZERO jobs) | T-DB3 (+ T-EQ2 at unit level) | the `succeeded`-run SELECT in `enqueue_provisioning_job` |
| b | lease-reaper / terminal-status (crashed `running` run re-opened + marked failed, NEVER left running) | T-DB4 (+ T-DB5) | the reaper's `ingest_run.status='failed'` write |
| c | intra-job `(document_id, chunk_content_hash)` no-op (re-run skips scraped chunks) | T-DB9 (+ T-OR7) | the frozen SELECT-skip is exercised; the orchestrator's count RE-ASSIGN (not `+=`) is the WU-3B2g-owned half |

## 12. Verification commands

Interpreter: `.venv/Scripts/python.exe` (Docker up for the real-PG file). Run from repo root `ai-ta-backend/`.

```bash
# Unit (pure / savepoint):
.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_enqueue.py \
  apollo/provisioning/tests/test_orchestrator.py \
  apollo/provisioning/tests/test_promote.py \
  apollo/tests/test_provision_worker.py -q

# Real-PG (green-NOT-skipped — a skip is a FAIL of the real-infra gate):
.venv/Scripts/python.exe -m pytest tests/database/test_apollo_provisioning_orchestration.py -q

# Non-regression (existing teacher-upload + provisioning suites stay green):
.venv/Scripts/python.exe -m pytest tests/database/test_teacher_upload_checkpoint_postgres.py \
  tests/database/test_apollo_provisioning_queue.py apollo/provisioning -q

# Broad apollo + db sweep:
.venv/Scripts/python.exe -m pytest tests/database -q
.venv/Scripts/python.exe -m pytest apollo -q

# Patch-coverage gate (the binding 95% on changed lines):
.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml -q
.venv/Scripts/python.exe -m diff-cover coverage.xml \
  --compare-branch=feat/apollo-kg-wu3b2f-queue-metered --fail-under=95
```

Manual smoke (the pipeline-shape end-to-end, optional): insert a `pending` job + `queued` run on a local DB, set `APOLLO_AUTOPROVISION_ENABLED=1`, run one `_run_one_iteration` with mocked stages → assert the run flips to `succeeded`/`failed` and the job to `completed`/`failed`.

Dry-run cost calculation (formula, not a real call): per document ≈ `n_chunks` cheap scrape calls + `n_candidates × (1 main generate + 2 cheap judge + 1 cheap tag)`; cost = `cost_usd_for(model, tokens_in, tokens_out)` summed; abort when cumulative tokens > `PER_DOCUMENT_TOKEN_CEILING` (2,000,000). In CI all calls are mocked → $0.

Replay test (idempotency): T-OR7 + T-DB9 run the same input twice and assert no inflation. DLQ test: T-DB4/T-DB5 inject a failing input and assert it lands in `apollo_provisioning_jobs.state='failed'` + `apollo_ingest_errors`. Backpressure test: T-OR6 injects a `CostBudgetExceeded` and asserts graceful failure (the per-document ceiling is the backpressure valve; the worker's `SWEEP_LIMIT=1` + `POLL_SECONDS` bound throughput against the OpenAI rate limit).

## 13. Owner-doc reconciliation (same commit)

The drift contract requires reconciling the owner docs IN THE SAME COMMIT and bumping `last_verified` to **2026-06-20**. The owner-doc boundary crosses three docs (the contract's flag) plus a fourth-doc note:

- **`docs/architecture/apollo.md`** (`owns: apollo/**` — already covers `apollo/provisioning/**` + `apollo/provision_worker.py`; `last_verified` currently 2026-06-20, keep/confirm). Register, in the `apollo/provisioning/` module-map row (line ~39) and a short prose note:
  - `enqueue_provisioning_job(session, *, search_space_id, document_id, content_hash) -> int | None` (idempotent enqueue: content-hash short-circuit + partial-unique-index).
  - `run_provisioning(db, neo, *, job, metered_chat, embed_fn=, retrieve_fn=) -> ProvisioningOutcome` (the 6-stage spine; the §4b stage-outcome→observability decision; counts re-assigned not incremented).
  - `promote(db, neo, *, problem, mint_plan, search_space_id, concept_problem_id, existing_problem_hashes) -> PromoteResult` (annotate → `run_promotion_lint` → tier 1→2 + payload + `project_canon`; FAIL → orchestrator writes `apollo_rejected_problems`).
  - `apollo/provision_worker.py` — the dormant worker SHELL (mirrors `learner_janitor_worker.py`); flag `APOLLO_AUTOPROVISION_ENABLED` default OFF; the lease-reaper (§9 OPS-5: an expired `running` job re-opens + its run marked failed, never left running).
- **`docs/architecture/domain-data.md`** (`owns: knowledge/**` etc.; `last_verified` 2026-06-19 → 2026-06-20). Update the `knowledge/teacher_weekly.py` row to note the THIN §8B enqueue hook at the indexing-completion seam (`enqueue_provisioning_job` inside the finalize commit, idempotent, no-op when the flag is OFF because nothing drains). The migration-030 / ORM narration already exists (frozen 3B2a); add that 3B2g WRITES `apollo_ingest_runs`/`apollo_provisioning_jobs`/`apollo_rejected_problems`/`apollo_ingest_errors` at runtime.
- **`docs/architecture/indexing.md`** (`owns: indexing/**`; `last_verified` 2026-06-12 → 2026-06-20). Add a note at the completion seam: WU-3B2g's enqueue READS the existing already-embedded `aita_chunks` (it does NOT re-index), and the auto-provisioning idempotency is content-hash-based (`AITADocument.content_hash` short-circuit at enqueue; `chunk_content_hash` intra-job) so a re-index is a no-op for provisioning.
- **`docs/architecture/_overview.md` (boundary note — NOT necessarily edited by this unit, but the Procfile lives here).** The `Procfile` and the worker-process registry (`apollo-janitor flow`, §137) are owned by `_overview.md`, not apollo.md. The binding constraint enumerates apollo.md/domain-data.md/indexing.md for reconciliation; the `apollo-provision` process line + "scaled to 0 replicas until `APOLLO_AUTOPROVISION_ENABLED` is flipped" deploy note SHOULD be added to `_overview.md`'s Procfile row + a 4th-process flow paragraph mirroring the apollo-janitor one. **Flag for the orchestrator:** editing `_overview.md` is the drift-correct action (its `owns: Procfile` makes it the authority on the line this unit adds) but it is OUTSIDE the three docs the binding constraint names. RECOMMENDATION: include the `_overview.md` Procfile/process edit in the same commit (it is the doc that owns `Procfile`); if the harness forbids touching `_overview.md`, leave a `TODO(_overview.md): register apollo-provision process` note in `apollo.md` and surface it as a follow-up.

Verify: `grep -n "apollo-provision\|enqueue_provisioning_job\|run_provisioning\|provision_worker" docs/architecture/*.md` returns the new registrations; `grep -n "last_verified: 2026-06-20" docs/architecture/{apollo,domain-data,indexing}.md` is present for all three.

## 14. Risks (confidence-rated)

- **[HIGH confidence it matters] Flag-OFF non-regression is the gate.** If the thin enqueue can ever raise out of the finalize commit, it breaks every teacher upload. Mitigation: T-DB8 proves an enqueue `IntegrityError` is swallowed via `begin_nested`; T-DB6 proves the upload-facing columns are byte-identical with the flag OFF. The enqueue must do the LEAST possible inside the upload session.
- **[HIGH] Terminal-status / lease-reaper correctness (§9 OPS-5).** A run left `'running'` wedges re-enqueue via the partial-unique-index. Mitigation: the reaper marks the run failed on every expired-lease sweep (T-DB4), and the orchestrator's per-document error path always flips the run to a TERMINAL status (T-DB5) — never an early-return that leaves `running`. Confidence the design closes it: high; the residual is a worker that dies between `complete_job` and the run's `finished_at` write — mitigated by writing `status` BEFORE returning the outcome (the worker's terminal call is idempotent on an already-terminal run).
- **[MEDIUM] Session/transaction boundary between the orchestrator and the worker.** `run_provisioning` commits the run/observability writes; the worker then calls `complete_job`/`fail_job` (which commit the job). A crash between them leaves the run terminal but the job `running` — re-claimed on lease expiry, the re-run is idempotent (counts re-assigned, scrape skips, promote no-ops), so the only cost is a re-run, never corruption. Documented as acceptable (mirrors the janitor's "claim-then-commit-before-work" posture). Confidence: medium-high.
- **[MEDIUM] Real-PG harness reuse.** The committed-engine harness in `test_apollo_provisioning_queue.py` must be ADAPTED (not copied verbatim) — extend the post-test `DELETE` set to include `apollo_concept_problems`/`apollo_kg_entities`/`apollo_concepts`/`apollo_subjects`/`apollo_rejected_problems`/`apollo_dedup_decisions` (the orchestration test writes more tables than the queue test). The chain selector `_TOUCHES_TARGETS` already resolves `[009,018,025,026]→030` and needs no change. Risk: a missed cleanup table leaks rows across tests → flaky. Mitigation: enumerate every table the orchestrator writes in the teardown.
- **[MEDIUM] `retrieve_fn` default for find_or_generate.** The orchestrator must inject a course-corpus retrieval adapter for `find_or_generate`/`validate_pair`. In Tier-1 tests it is MOCKED; the real adapter (over `retrieval/` hybrid pipeline) is the one place WU-3B2g adds runtime wiring beyond pure orchestration. Risk: the real adapter's signature drift. Mitigation: inject it as a parameter (default a thin adapter), keep the adapter <20 lines, and unit-test the orchestrator only against the mock (the real adapter's correctness is `retrieval/`'s contract, exercised at Tier-2 nightly).
- **[LOW] Cost surprise.** None per-PR (mocked). At runtime the `PER_DOCUMENT_TOKEN_CEILING` circuit-breaker (2M tokens/doc) bounds a runaway; the flag-OFF default means no spend until a human enables it. The Tier-2/Tier-3 real-LLM cost lives in 3B2h's nightly/release harness (scaffolded-not-run here).
- **[LOW] External API availability.** OpenAI down → stages raise → per-document failure → `apollo_ingest_errors` + retry on the next sweep (bounded by `MAX_ATTEMPTS`=3 dead-letter). No grade or student-facing path is affected (auto-provisioning is fully async + flag-OFF).
- **[LOW] Schema-lock during deploy.** None — NO migration in this unit (030 is frozen). The Procfile process is scaled to 0 replicas at deploy; no lock.
- **[LOW] `_overview.md` owner-doc boundary.** The Procfile is owned by `_overview.md`, outside the three named docs (§13). Flagged for the orchestrator to decide whether to include the `_overview.md` edit in-commit or as a follow-up TODO.

## 15. Out-of-scope boundaries (held firmly)

- **`apollo/handlers/done.py` / `done_grading.py` / `done_inputs.py` / the §6 grading core (`apollo/retired graph comparator/**`) — ZERO change.** Auto-provisioned Tier-2 problems flow through the EXISTING shadow path. WU-3B2g does not touch grading.
- **The stage modules (3B2b–3B2f) — REDEFINE NOTHING.** `scrape`/`find_or_generate`/`validate_pair`/`tag_and_mint`/`promotion_lint`/`problem_hash`/`dedup`/`queue`/`metered_chat`/`cost_constants` are FROZEN imports. WU-3B2g wires them; it does not edit them. (The one frozen-adjacent edit is `apollo/provisioning/__init__.py` re-exports — additive only.)
- **Migration / DDL — NONE.** Uses migration 030's frozen tables. NEVER apply a migration to any remote DB; the real-PG tests run on LOCAL Testcontainers only.
- **The §8 seed writer `scripts/seed_apollo_learner_model.py` — not touched.** `promote.py` REUSES `annotate_reference_solution`/`validate_reference_graph` (frozen pure converters); it does not import the disk-bound seed writer.
- **The anomaly quarantine + shadow verification (WU-3B2h) — out of scope.** `quarantined_at` is written by 3B2h; the selector's `quarantined_at IS NULL` clause is 3B2h's. WU-3B2g writes `tier=2` only.
- **Flipping `APOLLO_AUTOPROVISION_ENABLED` or `APOLLO_GRAPH_SIM_LAYER3_ENABLED` anywhere — out of scope.** Both stay OFF; activation is a human calibration/deploy step. The Procfile process ships scaled to 0 replicas.
- **Tier-2 nightly / Tier-3 release real-LLM runs — scaffolded-not-run.** No real OpenAI tokens in CI (§8B.7 Tier-1 only per-PR).
- **A new LLM provider / LangChain / Assistants API / new vector store — forbidden.** OpenAI-only via the frozen `MeteredChat`; pgvector/Neo4j only.
- **Teacher-UI surfacing of provisioning status — out of scope** (backend-only; a dashboard is a separate teacher-ui follow-up).
- **Real Neo4j Testcontainer for `project_canon` — not required** (mocked; `project_canon` is frozen with its own WU-3C1 Neo4j tests). No test file lands under `apollo/knowledge_graph/`.

## 16. Deviations the executor may take

- **Worker-test file location** — `apollo/tests/test_provision_worker.py` (recommended, mirrors the janitor) OR fold the worker-shell pure-unit cases into the in-scope `tests/database/test_apollo_provisioning_orchestration.py` if the allowlist is strict-set (§9 note). Either is acceptable provided the worker body carries real tests.
- **`_overview.md` Procfile/process registration** — include the edit in-commit (drift-correct, since `_overview.md` owns `Procfile`) OR leave a `TODO(_overview.md)` in `apollo.md` if the harness forbids touching `_overview.md` (§13 / §14).
- **`retrieve_fn` real adapter** — inject as a parameter with a thin default adapter over `retrieval/`; the executor may name/shape the adapter as fits the existing retrieval entrypoint, as long as the orchestrator unit-tests mock it and the adapter stays <~20 lines.
- **`LEASE_SECONDS` / `SWEEP_LIMIT` / `POLL_SECONDS` defaults** — the executor may pick env-var names/defaults consistent with the janitor's (`APOLLO_PROVISION_*`), provided they are `_int_env`-read and documented.
- **`ProvisioningOutcome` / `PromoteResult` as Pydantic `BaseModel` vs frozen dataclass** — either is fine (the frozen modules mix both); prefer the one that matches the immediate caller's convention.
- **Whether `run_provisioning` commits internally vs returns and lets the worker commit** — the plan specifies the orchestrator commits observability writes and the worker commits the terminal job transition; the executor may consolidate into a single session if the real-PG tests still prove the terminal-status + reaper invariants (the invariants are binding, the exact commit granularity is not).
- **The promote annotation detail** — if `annotate_reference_solution` needs a `key_for_node` callable the executor cannot cleanly source from the `MintPlan`, it may build the `entity_key` map directly from `mint_plan.minted_entity_ids`/`merged_entity_keys` as long as `run_promotion_lint` receives a graph dict carrying `entity_key` per step + `declared_paths` (the lint's documented input contract).

**NOT deviable:** the three mutation proofs (a/b/c) must RED on guard removal; the flag-OFF byte-identical upload non-regression; ZERO `done.py` change; NO migration; NO remote DB; the 95% patch-coverage gate vs `feat/apollo-kg-wu3b2f-queue-metered`; the same-commit owner-doc reconciliation with `last_verified=2026-06-20`.
