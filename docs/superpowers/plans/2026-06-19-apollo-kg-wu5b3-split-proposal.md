# WU-5B3 split proposal (FINAL, build-ready) — the `learner_update_pending` retry janitor

**Date:** 2026-06-19
**Author:** split-planning pass (autonomous stacked-PR build) — REVISER role. Three critics (FEASIBILITY, SPEC-FIDELITY+IDEMPOTENCY, OPS/CONCURRENCY) reviewed the prior draft; every valid finding is folded in below with each disputed quote/line re-verified against the ACTUAL code on this read (2026-06-19).
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` — §3 concurrency/retry (L456-464), §6.4 "the retry re-runs FROM resolution idempotently" (L744-754), §5 NO-FALLBACK, §7 retention/Aura-pause, §3 Δt-anchor "never now()" (L416-418)
**Parent split:** `docs/superpowers/plans/2026-06-18-apollo-kg-wu5b-split-proposal.md` + its two ORCHESTRATOR ADJUDICATION blocks (2026-06-18). WU-5B3 was PRE-SCOPED + ADJUDICATED there; this document VALIDATES that pre-scoping against the *current* code, DEEPENS the design, and decides the INTERNAL split of WU-5B3.
**Status:** PROPOSAL — FINAL (critic-revised). No code/tests/source touched. Every load-bearing claim re-reproduced against the ACTUAL code at the current branch tip (`feat/apollo-kg-wu5b3-retry-janitor`, parent `c1b6d92`). Two pre-scoping facts found STALE/over-stated and corrected (the `pool_recycle` framing; the clear-flag location); one make-or-break re-derivation bug surfaced (`current_problem_id` vs `attempt.problem_id`); one missing artifact added (a new named dead-letter error); one extra dead-letter trigger added (empty Neo4j subgraph). The critic pass corrected FOUR additional defects in the prior draft: (a) the short-txn diagram falsely claimed "NO held connection" during the LLM work (the WORK session IS held); (b) the test-harness story conflated the Neo4j-STUBBED route harness with real-Neo4j and could not exercise SKIP-LOCKED contention; (c) the claim had no lease, leaving a double-WORK window; (d) the opportunistic backstop sat on the Done hot path. All four are resolved below.
**Branches off:** `feat/apollo-kg-wu5b2-negotiation-multiplier` (#41 tip). The current branch is `feat/apollo-kg-wu5b3-retry-janitor`. Open stacked DRAFT PRs; never merge. `diff-cover --compare-branch=feat/apollo-kg-wu5b2-negotiation-multiplier --fail-under=95`.

---

## 0. Branch reality — what is ALREADY merged (and what the pre-scoping mis-states about file edits)

**WU-5B1 (decay) and WU-5B2 (negotiation multiplier) are ALREADY MERGED into this branch** — they are NOT just "frozen upstream you build on." Git log confirms (`f51da69` WU-5B1, `59f1df8` WU-5B2). The consequence the executor MUST internalise: the frozen-file edits the *parent* WU-5B proposal attributed to 5B3 (and which the prompt's pre-scoping echoes) are **already present in the tree** — `apollo/learner_model/persistence.py` and `apollo/handlers/learner_update.py` already carry:

- `persistence.py` — the three identity-default injected kwargs (`prior_transform`, `likelihood_multiplier_for_entity`, `negotiation_move_for_entity`) AND the dt-hoist (`dt_days_since_last` computed at the TOP of `_combined_belief_update`). Verified.
- `learner_update.py:46-62,98-145` — the decay + negotiation closures (`_learner_decay_enabled`/`_learner_negotiation_enabled` at `:49-62`, the closures built at `:104-130`), both flags, all threaded into `persist_learner_update` at `:132-143`. Verified.

So WU-5B3 does **NOT** re-edit the belief fold. The ONLY nominally-frozen file 5B3 must touch is the **clear-flag** — and §3 below DISSENTS from the pre-scoping on WHERE that line goes (the pre-scoping puts it inside `run_learner_update`; this proposal puts it in the janitor's own accounting txn to preserve the WU-5A2 byte-identity guardrail). This single seam decision is the most important call in the whole unit and is surfaced as `open_decisions_for_orchestrator` #1.

**The supersede-idempotency guarantee 5B3 leans on is shipped and verified** (`persistence.py`): attempt-wide `DELETE FROM apollo_mastery_events WHERE attempt_id = :id` runs FIRST (`:297-299`); `_recompute_base_for_entity` reads the prior-attempt event log via `MasteryEvent.attempt_id.is_distinct_from(attempt_id)` ordered `desc(created_at), desc(id) LIMIT 1` (`:168-180`), with the docstring explicitly stating it NEVER reads the base off `LearnerState.belief` to avoid double-counting a re-run (`:165-167`, re-read this pass — verbatim). A janitor re-run is therefore naturally idempotent in final belief state — **RATIFIED**.

**The SHADOW re-persist is ALSO supersede-idempotent (critic-confirmed this pass).** `persist_comparison_run` is NOT append-only: it `DELETE FROM apollo_graph_comparison_runs WHERE attempt_id = :id AND comparison_version = :v` FIRST (the findings CASCADE-drop), then re-inserts, all in one txn (`grading/persistence.py:229-244`). So a janitor re-run that re-calls `run_graph_simulation` does NOT leak duplicate comparison-run/findings rows on each retry — the LAYER3-OFF re-defer loop (which re-runs the shadow every drain, §3) is therefore bounded in row count, not unbounded growth. The FEASIBILITY-critic concern that this might be append-only is RESOLVED against the code.

---

## 1. Ground truth re-verified for the janitor (load-bearing facts, file:line — re-read 2026-06-19)

### What sets the flag, what is durable before it
- **TWO no-fallback setters, both re-raise the ORIGINAL infra error (no janitor sentinel exists).** Site A — the shadow chain: `done_grading._set_pending_and_commit` (`:156-162`) fired from `except _PENDING_ON_ERRORS` (`:313-317`) and `except Exception` (`:318-322`); validation failures (`ReferenceGraphInvalidError` 409 `:304-308`, `StudentGraphInvalidError` 422 `:309-312`) re-raise WITHOUT flagging. Site B — the Layer-3 txn: `learner_update.py:146-151` does `await db.rollback()` (`:149`) → `_set_pending_and_commit` (`:65-71`) → `raise`, on ANY exception inside the single commit (`:144`). **Implication:** the janitor catches a GENERIC exception around the re-run, not one sentinel type; it distinguishes the *unreconstructable* case by its OWN pre-flight check (missing `old_rubric` / missing `graded_at`), never by exception type.
- **By the time either setter fires, these are committed** (`done.py`): the OLD grade + `attempt.result="graded"` + **`attempt.diagnostic_report = {narrative, rubric, coverage}`** at `:335-339`, committed `:341`; XP via `apply_xp` `:343-347`; Neo4j `graded_at` via `stamp_graded_at(ts=done_ts)` `:366`; the shadow run/findings inside `run_graph_simulation` (`done_grading.py:256`). So the `old_rubric` source is durable BEFORE the failure window. `done.py:335` is the ONLY writer of `diagnostic_report` in the codebase.
- **Nothing clears the flag.** `run_learner_update`'s success path (`learner_update.py:144-145`) commits the belief write and returns — it does NOT set `pending=False`. No janitor module exists (`apollo/handlers/learner_janitor.py`, `apollo/learner_janitor_worker.py`, `apollo/handlers/done_inputs.py` all absent). **The single most important new write in 5B3 is the clear-flag.**

### The re-run is NOT a cheap DB fold — the shadow is ephemeral
- `ShadowGradeResult` (`done_grading.py:91-113`) is an in-memory `@dataclass(frozen=True)`; only `run_id` is durable. `persist_learner_update` consumes `shadow.audited` / `shadow.opposes_map` / `shadow.turn_order` / `shadow.normalization_confidence` (`persistence.py:282-292`); the persisted `apollo_graph_comparison_findings` rows do NOT carry `abstained`/`suppressed_event_kinds`/`opposes_map`/`turn_order`. There is NO reverse loader. So the janitor MUST re-run `run_graph_simulation` from resolution — `resolve_attempt(..., llm_adjudicator=main_chat_adjudicator)` (`done_grading.py:205-211`) + `build_audited_grade(..., audit_fn=main_chat_auditor)` (`:230-238`) = 2 LLM calls + Neo4j MERGE (`write_resolution`, `:213`) + a `db.commit()` of the comparison run (`:256`). Spec confirms intent (L744-754).

### `old_rubric` is a HARD-REQUIRED re-run input (the HIGH bug — RATIFIED, re-verified verbatim this pass)
- `run_graph_simulation`'s signature requires `old_rubric: dict` (keyword-only, no default, `done_grading.py:173`). It flows to `compute_calibration_metrics(old_rubric=…, shadow_rubric=…)` (`done_grading.py:276-278`), which dereferences `old_rubric["overall"]["letter"]` (`calibration.py:88`) and `old_rubric["overall"]["score"]` (`calibration.py:93`) **UNCONDITIONALLY** — no `.get()`, no guard, no try (re-read `calibration.py:80-94` this pass — confirmed exact). A `None`/`{}` `old_rubric` raises `KeyError`/`TypeError` on EVERY drain.
- **Source: `attempt.diagnostic_report["rubric"]`** (`done.py:335-339`, JSONB `models.py:275`), durable before the failure window. The janitor reads it rather than re-running the OLD coverage/rubric path. **Dead-letter trigger:** `diagnostic_report is None` (attempt errored before `done.py:341`) OR `"rubric" not in diagnostic_report` (legacy shape) OR `diagnostic_report["rubric"]` missing `["overall"]`. Never crash-loop.

### The `done_ts` source is Neo4j `graded_at` — no PG column, no migration (RATIFIED)
- `ProblemAttempt` has NO `graded_at` Postgres column. **Path correction:** the model lives at `apollo/persistence/models.py`, NOT `apollo/models.py` (the prior draft cited a non-existent path). `ProblemAttempt` (`apollo/persistence/models.py:265-285`) carries only `id, session_id, problem_id, difficulty, result, solver_trace, diagnostic_report, learner_update_pending, created_at` — re-read this pass; no `graded_at`. Verified.
- `stamp_graded_at` SETs `n.graded_at = $ts` on every `:_KGNode {attempt_id}`, threaded the original `done_ts`, idempotent (`store.py:537-575`). A `datetime` is normalized via `.isoformat()` (`store.py:558-559`) → **the stored value is an ISO-8601 STRING**. `read_graph` STRIPS `graded_at` (`store.py:148`: `bag.pop("graded_at", None)`). So a NEW `KGStore.read_node_graded_at` is required — a near-exact mirror of `read_node_created_at` (`store.py:472-495`, re-read this pass): `MATCH (n:_KGNode {attempt_id:$aid}) RETURN n.node_id, n.graded_at`, None-omit. The janitor reads any one value (all nodes share `done_ts`) and **parses the ISO-8601 string via `datetime.fromisoformat`, asserting the result is tz-AWARE** (see the determinism risk row — a naive value would break the `(done_ts - last_evidence_at).days` Δt subtraction).
- **NEW dead-letter trigger (memo addition):** if the subgraph is empty / never stamped, `read_node_graded_at` returns `{}` → no `done_ts` → cannot anchor decay-Δt → **dead-letter**, alongside the `old_rubric`-missing case.

### The other re-run inputs are all DB/Neo4j-durable (re-derivability table — §5)
- `freeze` is **Postgres-only** (`store.py:247-253` sets `session.phase` only; no Neo4j mutation) → the frozen graph `read_graph` returns == the graph the original run consumed. Determinism is scoped to the **Δt anchor**, not belief byte-identity (the re-run re-calls the live LLM; §3 L462-464 blesses a differing re-grade).

### The host + the leasing idiom (RATIFIED) + the connection-lifetime lesson (corrected framing)
- `Procfile`: `web` + `worker: python -m teacher_upload_worker` (verified, 2 lines). `teacher_upload_worker.main()` runs `TeacherWeeklyStorage().run_upload_worker_loop()` — a SYNC `while True` (`teacher_weekly.py:351`) + `time.sleep(job_poll_seconds)` (`:360`). **PRAGMA CORRECTION (critic, verified this pass):** the `# pragma: no cover` is on the **`except KeyboardInterrupt:`** branch (`:355`), NOT on the `while True:` line and NOT on the loop body — `process_next_upload_job` (`:354`), the generic `except`/`log.exception` (`:357-358`), and the `time.sleep` (`:359-360`) are ALL reachable and ARE exercised by tests. This sets the bar for what 5B3b may legitimately exempt (see §3 5B3b).
- **Canonical claim idiom** `_claim_upload_job_async` (`teacher_weekly.py:881-935`, re-read this pass): opens its OWN short `get_async_session()` (`:888`), `select(...).order_by(created_at.asc(), id.asc()).with_for_update(skip_locked=True)` (`:901-902`), claims, **sets a LEASE `lease_expires_at = now + job_lease_seconds` (`:914`)** AND bumps `attempt_count` (`:915`), **`await session.commit()` IMMEDIATELY (`:925`)**, returns; the work runs in a SEPARATE call/session. The lease at `:914` is load-bearing: it drops the row out of the claim predicate (`lease_expires_at < now` at `:895-897`) for the duration of the work, preventing a second drainer from re-claiming the SAME row mid-work. **The prior draft's claim omitted this lease — corrected below (§3, OPS concurrency fix).**
- **Canonical record-txn idiom** `_handle_job_failure_async` (`teacher_weekly.py:1176-1210`, re-read this pass): opens its OWN `get_async_session()` (`:1183`), **RE-FETCHES the row by id via `session.get()` (`:1184-1185`) — never a stale ORM object from the work session**, computes `is_terminal = terminal or attempts >= self.max_retries` (`:1190`), logs an exhaustion WARNING (`:1192-1195`), and commits (`:1210`). This is the EXACT shape the janitor's record-txn must follow.
- **CORRECTION to the pre-scoping (STALE FACT):** `pool_recycle=1800` + `pool_pre_ping=True` + `pool_size=10` are ALREADY set (`session.py:85-94`, re-read this pass) — the textbook handoff's recommendation #3 already landed, and the engine comment itself states "the real fix is that indexing no longer holds a connection across the embedding loop." The pre-scoping/prompt imply it's missing. The short-session discipline is STILL mandatory and is the REAL cure: `pool_recycle` recycles a connection *checked back into the pool* after 30 min; it does NOT save a connection **held checked-out** across multi-minute LLM work (exactly the textbook failure mode, `docs/TEXTBOOK-WORKER-DB-CONNECTION-DROP-HANDOFF.md`). So the binding rule survives intact and is *strengthened*. Cite `pool_recycle` accurately as "already set, necessary-but-insufficient."

### MAKE-OR-BREAK re-derivation bug (VERIFIED — not in the pre-scoping)
- `done.py:262` finds the problem from `sess.current_problem_id` (`_find_problem(db, sess.concept_id, sess.current_problem_id)`); `done.py:268` then matches `attempt.problem_id == problem.id`. At live-Done time these coincide. BUT `next.py:94` advances `sess.current_problem_id = problem.id` on the NEXT problem, and `next.py:57` keys the new attempt lookup on `current_problem_id`. So when the janitor drains a pending attempt LATER, `sess.current_problem_id` may point at a DIFFERENT problem. The durable correct key is **`attempt.problem_id`** (the `problem_code` string — `_find_problem_payload` filters `ConceptProblem.problem_code == problem_code`, `done.py:247`). **`done_inputs.py` MUST key on `attempt.problem_id`, NEVER `sess.current_problem_id`.** If the builder is extracted by copying `done.py`'s `current_problem_id` path verbatim it grades the WRONG problem on every multi-problem session — invisible in single-problem fixtures. An explicit test (a session advanced PAST the pending attempt's problem) is mandatory.

### Migration baseline
- On-disk tops at `database/migrations/027_apollo_drop_concept_cluster_id.sql` (verified this pass: `ls` shows 022…027 + `__init__.py`, no 028). **Next free = 028.** Decay/hedge needed none (shipped). The janitor needs 028 for dead-letter + backoff columns + a partial index. `done_ts` needs none (Neo4j `graded_at`).

### Errors
- `apollo/errors.py` is rooted at `ApolloError` (`:10`); siblings `RetentionError` (`:153-166`, re-read this pass — the clean template), `CanonProjectionError`, `ResolutionUnavailableError` (`:169-186`), `TranscriptAuditUnavailableError` (`:189`). **There is NO `LearnerUpdateUnreconstructableError`.** It must be ADDED (a new `ApolloError` subclass mirroring `RetentionError`'s `__init__(self, *, attempt_id, ...)` shape) so the dead-letter path is a deliberate terminal state, not a caught generic crash. NOT in the pre-scoping's file list — added here (§3).

---

## 2. Recommendation (the internal split)

**Split WU-5B3 into TWO sub-units, build in this order:**

1. **WU-5B3a — the real-PG-tested CORE** (no process). Migration 028 + the new dead-letter error + `done_inputs.py` shared input-builder + `KGStore.read_node_graded_at` + `apollo/handlers/learner_janitor.py::drain_pending_attempts` (claim-LEASE, short-txn discipline, recompute via the frozen `run_graph_simulation`+`run_learner_update`, dead-letter+backoff, LAYER3 interlock, clear-flag). Fully Testcontainers-testable (real PG + real Neo4j; LLM stubbed). NO worker process.
2. **WU-5B3b — the THIN process/wiring layer.** The NEW dormant `apollo-janitor` worker (`apollo/learner_janitor_worker.py` + `Procfile` line) + the opportunistic backstop hook in `done.py` + the `APOLLO_LEARNER_JANITOR_ENABLED` flag read + SIGTERM `asyncio.Event` handler. Mostly a `# pragma: no cover` `while`-shell wrapping the 5B3a core.

**Why two, not one — adversarially evaluated.** Both research memos independently reached this and the pre-scoping pre-decided it; I RATIFY it, with the test-harness story CORRECTED (the prior draft over-claimed harness reuse — see the next paragraph). 5B3a is independently real-PG-testable with ZERO process code; the janitor core drives `run_graph_simulation`/`run_learner_update` with the SAME LLM stubs the route harness uses. 5B3b is a `while not stop_event` + `await sleep` shell that is `# pragma: no cover` ONLY on the un-driveable lines (the bare `asyncio.run` entrypoint and the `while` shell), carrying no testable logic beyond the flag-read + the interlock check + the opportunistic hook (all unit-testable in isolation, all REQUIRING real tests — they are NOT pragma-exempt). Bundling an uncoverable shell with the coverable core dilutes the 95% patch-coverage signal on 5B3a and invites "the loop is covered by an integration test" hand-waving. **Clean seam = clean coverage story.** A SINGLE unit is also rejected on size: the parent adjudication (#2) flagged the worker-loop + claim/lease/dead-letter/backoff + Neo4j read + migration 028 as the ceiling. Pulling the process wiring + opportunistic-backstop `done.py` edit + graceful shutdown into the SAME PR pushes it past XL. The seam is what keeps each PR a single TDD pass.

**Test-harness REALITY (critic correction — load-bearing for the 95% gate).** The prior draft claimed 5B3a "reuses `test_done_layer3_route_postgres.py` verbatim plus a `read_node_graded_at` stub" and is "real-PG + real-Neo4j." Both halves are FALSE as stated, and the executor would hit this day one. Verified this pass:
- **The route harness STUBS Neo4j.** `test_done_layer3_route_postgres.py:804-815` patches `KGStore.read_graph`, `KGStore.freeze`, `KGStore.stamp_graded_at`, `read_node_created_at`, `main_chat_adjudicator`, `main_chat_auditor` all with `AsyncMock`, and `handle_done` is called with `neo=object()` (`:864`). Neo4j is entirely mocked there. So a NEW `read_node_graded_at` (a real Cypher `MATCH`) CANNOT be validated by that harness — it needs the SEPARATE real `neo4j_client` conftest fixture (`conftest.py:228-239`, `Neo4jContainer("neo4j:5.25")`).
- **The `db_session` fixture cannot exercise SKIP LOCKED.** `db_session` (`conftest.py:162-192`) is a per-test NullPool engine with a single connection inside an outer transaction that is **never committed** (`join_transaction_mode="create_savepoint"`, teardown rolls back). The drain's claim does `FOR UPDATE SKIP LOCKED` + `commit()` on its OWN `get_async_session()`. A single shared savepoint connection (a) cannot make a claimed-and-committed row visible to a genuinely independent connection, and (b) cannot demonstrate a row lock being SKIPPED by a SECOND concurrent transaction. SKIP-LOCKED semantics are unobservable without two real, concurrent, COMMITTED transactions.
- **The correct harness for the contention/claim-visibility tests** is the raw `_pg_url` two-connection asyncpg pattern that ALREADY exists in-repo: `tests/database/test_apollo_learner_model_migration.py` opens its own asyncpg connections against a committed migrated schema (`_pg_url` → `asyncpg.connect`, e.g. `:78,87,123,435,472`). The janitor's contention test seeds + COMMITs a pending row on connection A, opens connection B, and asserts B's claim skips A's FOR-UPDATE-locked row.

**Net:** the 2-way split STANDS, but 5B3a's test plan composes THREE harness shapes, not one (this is the real source of its "heavy" sizing — see the contingency below):
  - **(H1) `db_session` savepoint monkeypatch** — for the single-logical-claimer drain tests: happy-drain+clear, dead-letter (old_rubric/empty-subgraph), backoff/MAX_ATTEMPTS accounting, inner-reflag accounting, LAYER3-OFF zero-events, Δt-anchor determinism. These STUB `read_node_graded_at`/`read_graph` (their assertion target is PG claim/clear/dead-letter/backoff state, not Neo4j I/O), mirroring how the route harness already stubs Neo4j.
  - **(H2) raw `_pg_url` two-connection** — for SKIP-LOCKED two-claimer contention + claim-commits-visible + the migration-028 chain test (à la `test_apollo_learner_model_migration.py`). COMMITTED rows, two independent connections.
  - **(H3) real `neo4j_client` fixture** — for `read_node_graded_at` itself: stamp nodes, assert the helper returns the value; assert `{}` on an unstamped/empty subgraph (the empty-subgraph dead-letter trigger).

**Why NOT a 3-way split of 5B3a** (the candidate: migration+claim / recompute+dead-letter / shared input-builder). REJECTED as the DEFAULT. The claim core is meaningless without the recompute it claims FOR (the first PR would ship a claim with nothing to hand the row to); the claim and recompute are two halves of ONE `drain_pending_attempts` state machine sharing the short-txn discipline. The 3-way fractures that state machine. **5B3a is cohesive: one orchestration function calling two frozen entrypoints, plus a 5-column migration, a ~30-line extraction, a ~24-line Neo4j helper, and a ~12-line error class.** It is the heaviest unit in the 5B stack but not XL. (NOTE: the prior draft asserted the input-builder "has no independent test surface" — that is over-stated; migration 028 has its own H2 chain test and `read_node_graded_at` has its own H3 test, so a behavior-preserving pre-step DOES have testable surface. This is why the contingency below is upgraded from "only if XL" to "recommended IF the executor confirms the three-harness cost.")

**Adversarial caveat — the recommended pre-step IF the three-harness cost confirms 5B3a XL.** Given the H1+H2+H3 harness composition (three distinct test shapes) PLUS migration 028's own chain test PLUS the `done.py` shadow-input refactor (whose regression proof is the entire WU-5A2/4C live-path suite staying green), 5B3a sits at the very top of the size band. The natural sub-seam — and it now HAS independent test surface, contra the prior draft — is:
  - **5B3a-0** = the behavior-preserving foundation: `done_inputs.py` extraction (a pure `done.py` refactor, proven by the unchanged WU-5A2/4C live-path suite) + `KGStore.read_node_graded_at` (proven by its own H3 real-Neo4j test) + the new `LearnerUpdateUnreconstructableError` class + migration 028 (DDL only, proven by its own H2 migration-chain test). Independently green; lowers the per-PR TDD blast radius.
  - **5B3a-1** = the drain state machine: claim-LEASE + recompute + dead-letter/backoff + LAYER3 interlock + clear-flag + the H2 two-connection contention test.
  This is the recommended cut IF 5B3a sizes XL on the three-harness cost; the default remains the 2-way split with 5B3a as one unit. Surfaced in `open_decisions_for_orchestrator` #6.

**The first unit is WU-5B3a.** It carries all the correctness; 5B3b cannot be written until `drain_pending_attempts` exists to wrap.

### The v1-vs-defer call per concern

- **Worker-loop drain → v1-IN.** The parent adjudication (#2a) ruled opportunistic-only unacceptable (a never-returning student's mastery stays stale forever; injects 2 LLM calls into session-init). Ships as a NEW dormant `apollo-janitor` process, `APOLLO_LEARNER_JANITOR_ENABLED` default OFF + the LAYER3 interlock. Whether prod runs the process (and at what replica count) is a deploy decision; the code ships gated OFF.
- **Opportunistic backstop → v1-IN but POST-COMMIT, bounded, separately-controllable (critic-revised, default-defer recommended).** See OPS HIGH below: the backstop must run AFTER the student's grade is committed, on its OWN `get_async_session`s, wrapped in `asyncio.wait_for` with a small timeout, swallowing timeout/errors to log. It is a latency optimization, NEVER a correctness dependency (the worker is the correctness path). The OPS critic recommends DEFER; surfaced as a genuine open decision (`open_decisions_for_orchestrator` #7).
- **Migration 028 → v1-IN (5B3a).** Dead-letter + backoff columns + partial index only.

A one-unit collapse and a 3-way 5B3a split were both considered and rejected above.

---

## 3. Sub-units

### WU-5B3a — the retry-janitor CORE (real-PG-tested, no process)

**One-line:** A short-txn `drain_pending_attempts` that CLAIMS one pending attempt with `FOR UPDATE SKIP LOCKED` and sets a short LEASE in the same claim-txn (committing immediately), re-assembles the re-run inputs via a shared `done_inputs.py` (keyed on `attempt.problem_id`, sourcing `old_rubric` from `diagnostic_report` and `done_ts` from Neo4j `graded_at`), re-runs the frozen `run_graph_simulation` + `run_learner_update` in a FRESH WORK session, then in a SECOND fresh record-txn (re-SELECTing the row by id) clears the pending flag on success or records dead-letter/backoff on failure — dead-lettering immediately (no LLM call) when the inputs are unreconstructable.

**SEAM (owns vs 5B3b):** owns the migration, the claim/lease/accounting SQL, the input re-assembly, the Neo4j `graded_at` read, the dead-letter error + predicate, the backoff arithmetic, the LAYER3 interlock check, and the clear-flag write. It does NOT own the `while`-loop, the Procfile, the SIGTERM handler, or the opportunistic `done.py` hook (5B3b). It CALLS frozen `run_graph_simulation` + `run_learner_update`; it re-implements NO fold.

**Scope — files NEW:**
- `database/migrations/028_apollo_learner_janitor.sql` (NEW) — DDL in the migration block below.
- `apollo/errors.py` (EDIT, +~12 lines) — NEW `LearnerUpdateUnreconstructableError(ApolloError)` carrying `attempt_id` + `reason` (`"diagnostic_report_missing" | "rubric_missing" | "graded_at_missing"`), mirroring `RetentionError`'s `__init__(self, *, attempt_id, ...)` shape (`errors.py:161`). Raised by the janitor's pre-flight, caught by the dead-letter path. (NOT in the pre-scoping file list — added.)
- `apollo/handlers/done_inputs.py` (NEW, ~40 lines) — `async build_rerun_inputs(db, neo, *, attempt, sess) -> RerunInputs` (a frozen dataclass: `problem_payload`, `old_rubric`, `student_graph`, `done_ts`, `parser_confidence`). Sources: `problem_payload = await _find_problem_payload(db, concept_id=sess.concept_id, problem_code=attempt.problem_id)` (**`attempt.problem_id`, NOT `current_problem_id`**); `old_rubric = attempt.diagnostic_report["rubric"]` (raises `LearnerUpdateUnreconstructableError` if `diagnostic_report` is None / `"rubric"` absent / `["overall"]` absent); `student_graph = await store.read_graph(attempt_id=attempt.id)`; `done_ts` from `read_node_graded_at` **parsed via `datetime.fromisoformat` and asserted tz-aware** (raises if `{}`); `parser_confidence = min_parser_confidence_of(student_graph.nodes)` (`abstention.py:70`). The live `done.py` is refactored to call this builder for its own shadow inputs (drift-controlled — the WU-5A2/4C suite must stay green; that green suite IS the refactor's regression proof).
- `apollo/knowledge_graph/store.py` (EDIT, +~24 lines) — `KGStore.read_node_graded_at(*, attempt_id) -> dict[str, str]`, a mirror of `read_node_created_at` (`store.py:472-495`) returning `graded_at` (ISO-8601 strings), None-omit. Read-only helper, NOT a schema change. (The owner doc for `store.py` is `apollo.md`.)
- `apollo/handlers/learner_janitor.py` (NEW) — `async drain_pending_attempts(neo, *, limit, max_attempts[, user_id]) -> DrainResult` (a frozen dataclass: `claimed`, `succeeded`, `dead_lettered`, `retried`, `backlog_remaining`). Per the leasing idiom: **(claim-txn)** open a short `get_async_session()`, `SELECT … WHERE learner_update_pending AND NOT learner_update_failed_permanently AND (learner_update_next_attempt_at IS NULL OR learner_update_next_attempt_at <= now()) ORDER BY created_at ASC LIMIT :limit FOR UPDATE SKIP LOCKED`, **set `learner_update_next_attempt_at = now() + CLAIM_LEASE_S` (the short work-lease, drops the row out of the claim predicate while work runs)**, bump `learner_update_attempts += 1`, `commit()` immediately; **(WORK session — a 3rd `get_async_session` passed as `db` into the frozen callees)** for each claimed attempt build inputs via `done_inputs.build_rerun_inputs`, call `run_graph_simulation` then (if LAYER3 enabled) `run_learner_update`; **(record-txn)** a fresh session: **RE-SELECT the attempt by id** (never reuse the work-session ORM object), on success set `learner_update_pending = False`; on `LearnerUpdateUnreconstructableError` set `learner_update_failed_permanently = True` + `last_error` (no backoff, terminal); on other failure compute the exponential `next_attempt_at` (OVERWRITING the work-lease) + set `last_error`/`failed_at`, and flip `failed_permanently` at `attempts >= max_attempts`. The accounting txn is IDEMPOTENT w.r.t. the inner `_set_pending_and_commit` re-flag (the inner setter ran in the WORK session and already re-committed `pending=True`; the record-txn re-reads the row and does not assume sole authorship of `pending`).

**Scope — files EDIT (already-shipped frozen — see §0; NO further edit needed):** none of `persistence.py` / `learner_update.py`'s fold is touched. **The clear-flag is written by the janitor's own record-txn, NOT inside `run_learner_update`** (DISSENT from the pre-scoping — `open_decisions_for_orchestrator` #1).

**depends_on:** WU-5B2 (#41 tip — already merged), WU-5A2. Consumes the frozen `run_graph_simulation` (`done_grading.py`), `run_learner_update` (`learner_update.py`), `Neo4jClient`, `min_parser_confidence_of`, `_find_problem_payload`.
**real_infra:** **real-PG (Testcontainers `pgvector:pg16`) for the claim/lease/clear/dead-letter/backoff SQL + the migration; real-Neo4j (`neo4j:5.25`, the `neo4j_client` fixture) for `read_node_graded_at`. LLM STUBBED** (deterministic `main_chat_adjudicator`/`main_chat_auditor`, mirroring `test_done_layer3_route_postgres.py:813-814`). NO worker process. **Harness composition: H1 `db_session` savepoint (single-claimer drain/accounting tests, Neo4j stubbed), H2 raw `_pg_url` two-connection (SKIP-LOCKED contention + claim-visibility + migration chain), H3 `neo4j_client` (the `read_node_graded_at` helper).** See §2 test-harness reality.
**migration:** **028** (below).
**v1-or-defer:** **v1-IN.**

**MIGRATION 028 — EXACT DDL (Testcontainers-ONLY, NEVER remote):**
```sql
-- 028_apollo_learner_janitor.sql  (next_free = 028)
-- WU-5B3a — dead-letter + exponential-backoff columns for the learner_update
-- retry janitor. NO graded_at column: the original done_ts is durably on every
-- frozen Neo4j node (store.stamp_graded_at), read back via read_node_graded_at.
-- max_attempts default is 3 (NOT a queue-library 25) because each retry burns
-- 2 GPT-4o calls + re-commits the comparison run — the cap bounds duplicated
-- LLM SPEND, not just poison loops. Do not "fix" it up to a library default.
ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_attempts          INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS learner_update_failed_at         TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_last_error        TEXT        NULL,
    ADD COLUMN IF NOT EXISTS learner_update_next_attempt_at   TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_failed_permanently BOOLEAN    NOT NULL DEFAULT false;

-- Cheap drain scan: a PARTIAL index over only the pending, not-dead rows. The
-- index predicate uses ONLY the two boolean columns — `next_attempt_at <= now()`
-- is NOT in the predicate (now() is non-immutable / not index-predicate-legal)
-- and is applied as a RUNTIME query filter instead.
CREATE INDEX IF NOT EXISTS apollo_problem_attempts_pending_idx
    ON apollo_problem_attempts (created_at)
    WHERE learner_update_pending AND NOT learner_update_failed_permanently;
```

**Dead-letter + exponential-backoff schedule (PINNED — formula + constants + worked example):**

Constants (module-level in `learner_janitor.py`):
| constant | value | rationale |
|---|---|---|
| `MAX_ATTEMPTS` | `3` | each retry = 2 LLM calls + comparison-run re-commit; bounds duplicated SPEND. Matches the upload worker's 3-attempt exhaustion (`teacher_weekly.py:1190`). |
| `BASE_DELAY_S` | `300` (5 min) | first backoff after the initial Done-set pending. |
| `BACKOFF_FACTOR` | `4` | aggressive spacing on an expensive job. |
| `MAX_DELAY_S` | `3600` (1 h) | cap. |
| `JITTER` | `±10%` | thundering-herd defense for the Aura-pause recovery burst; computed in Python (`random`) so it is monkeypatch-deterministic in tests. |
| `CLAIM_LEASE_S` | `600` (10 min) | the work-lease written at claim — drops the claimed row out of the claim predicate while the 2-LLM work runs, preventing a second drainer/backstop from re-claiming the SAME row mid-work. Mirrors the upload worker's `lease_expires_at` (`teacher_weekly.py:914`). Overwritten by the real backoff (or cleared) in the record-txn. |

Formula (computed in the record-txn, app code, NOT SQL):
```
delay = min(MAX_DELAY_S, BASE_DELAY_S * BACKOFF_FACTOR ** (attempts - 1)) * (1 + uniform(-JITTER, +JITTER))
next_attempt_at = now() + delay
```
where `attempts = learner_update_attempts` (already incremented at claim). Interpreter-verified: `300·4^0=300`, `300·4^1=1200`, `300·4^2=3600` (capped).

Worked example (a row that fails every drain; jitter shown at its midpoint = 0):
| drain | `attempts` after claim | `delay` (no jitter) | terminal state |
|---|---|---|---|
| 1 (initial pending set by Done) | 1 | `300 · 4^0 = 300s` = **5 min** | pending, retry due in ~5 min |
| 2 | 2 | `300 · 4^1 = 1200s` = **20 min** | pending, retry due in ~20 min |
| 3 | 3 | — (`attempts >= MAX_ATTEMPTS`) | **`failed_permanently = true`** (terminal) |

Total spacing ≈ 5 + 20 = 25 min across 3 LLM-burning drains (interpreter-verified). A row that succeeds on drain 2 clears `pending` and never reaches the cap.

**Claim-lease SQL (PINNED — the exact claim-txn statements):**
```sql
-- (1) claim predicate — the LEASE (next_attempt_at) doubles as the in-flight guard:
SELECT * FROM apollo_problem_attempts
WHERE learner_update_pending
  AND NOT learner_update_failed_permanently
  AND (learner_update_next_attempt_at IS NULL OR learner_update_next_attempt_at <= now())
ORDER BY created_at ASC
LIMIT :limit
FOR UPDATE SKIP LOCKED;
-- (2) SAME claim-txn: set the work-lease + bump attempts, then COMMIT immediately:
UPDATE apollo_problem_attempts
   SET learner_update_attempts      = learner_update_attempts + 1,
       learner_update_next_attempt_at = now() + (:claim_lease_s || ' seconds')::interval
 WHERE id IN (claimed ids);
-- COMMIT;  -- releases the row lock AND the connection; the leased row now falls
--          -- out of predicate (1) until the work-lease expires or the record-txn
--          -- overwrites next_attempt_at with the real backoff (or clears pending).
```
(SQLAlchemy: `select(ProblemAttempt).where(...).order_by(ProblemAttempt.created_at.asc()).limit(limit).with_for_update(skip_locked=True)` — core, NO new package. The lease UPDATE uses `func.now() + timedelta(...)` bound params.) SKIP LOCKED makes web + worker + backstop single-flight WHILE a lock is held; **the work-lease (`next_attempt_at`) extends that single-flight guarantee ACROSS the unlocked work window** — without it, a second drainer could re-claim the SAME row the instant the claim-txn commits (the prior draft's gap).

**Short-txn boundary diagram (PINNED — the connection-lifetime contract, critic-corrected):**
```
  ┌─ claim-txn (short) ──────────────────────────────────────────────┐
  │  get_async_session() #1                                          │
  │   SELECT … FOR UPDATE SKIP LOCKED LIMIT :limit                    │
  │   UPDATE learner_update_attempts += 1,                           │
  │          learner_update_next_attempt_at = now() + CLAIM_LEASE_S   │
  │   COMMIT  ← releases the row LOCK AND the connection;             │
  │            the leased row drops out of the claim predicate        │
  └──────────────────────────────────────────────────────────────────┘
                 │  (NO FOR-UPDATE lock, NO uncommitted txn held)
                 ▼
  ┌─ WORK session #3 (a connection IS held checked-out here) ─────────┐
  │  get_async_session() #3 passed as `db` into the FROZEN callees:   │
  │   build_rerun_inputs (brief PG read for payload + Neo4j read_graph │
  │     + read_node_graded_at — short reads)                         │
  │   run_graph_simulation(db, neo, …)                               │
  │     → opens a multi-write txn, makes BOTH LLM calls (resolve      │
  │       :205, audit :230) INSIDE that txn, commits at :256          │
  │     → so a connection IS CHECKED OUT across the 2 LLM calls       │
  │   run_learner_update(db, …) (if LAYER3 enabled) → belief + commit │
  │  ⚠ THIS is the textbook-class hold — see the risk note below.     │
  └──────────────────────────────────────────────────────────────────┘
                 │  (work session closed)
                 ▼
  ┌─ record-txn (short) ─────────────────────────────────────────────┐
  │  get_async_session() #2 — RE-SELECT the attempt by id            │
  │   success      → learner_update_pending = False                  │
  │   unreconstructable → failed_permanently = True + last_error     │
  │   other failure → next_attempt_at = backoff + last_error/failed_at│
  │                   + (attempts >= MAX_ATTEMPTS → failed_permanently)│
  │   COMMIT                                                          │
  └──────────────────────────────────────────────────────────────────┘
```
**What is TRUE (and what is NOT):** the **claim-txn's `FOR UPDATE` row lock and any uncommitted txn are NEVER held across the 2 LLM calls** — the lock releases at the claim-commit. This is mechanically FORCED: `run_graph_simulation` calls `db.commit()` internally (`done_grading.py:256`), so a claim held in the same session would be committed early; the short-txn shape is required by the frozen callee. **BUT the WORK session (#3) DOES hold a Postgres connection checked-out across the 2 LLM calls** — the frozen `run_graph_simulation(db, …)` opens its txn before the LLM calls (`:203`) and commits after (`:256`), so the connection is not released during the LLM window. This is the SAME CLASS as the textbook-worker drop, just far shorter (the WORK session is seconds old at the first LLM call and the hold is minutes, not the textbook job's ~1.5h; the internal commit at `:256` also round-trips/refreshes mid-work). **Do NOT claim "no held connection."** Bounded mitigations (pin all three): (a) the WORK session is FRESH per drained attempt (minutes-scale hold, well under `pool_recycle=1800`); (b) `pool_pre_ping=True` validates at the NEXT checkout, so the record-txn (session #2) never reuses a dead socket; (c) on a mid-LLM connection drop the WORK statement after the LLM fails, the janitor's outer `except` catches it, and the record-txn backs the row off — acceptable because supersede makes the re-run idempotent. **Test (mirrors the textbook handoff's prescribed regression):** kill the connection between the LLM stub and the WORK-session commit and assert the row backs off cleanly (does not corrupt, does not crash-loop).

**old_rubric-missing dead-letter rule (PINNED):** `build_rerun_inputs` raises `LearnerUpdateUnreconstructableError(attempt_id, reason)` BEFORE any LLM call when `attempt.diagnostic_report is None` OR `"rubric" not in diagnostic_report` OR `"overall" not in diagnostic_report["rubric"]` OR `read_node_graded_at(attempt_id) == {}`. The janitor catches it in the record-txn and sets `learner_update_failed_permanently = True` + `last_error = reason` — terminal, NO backoff, NO LLM call, NEVER crash-loop. (Distinguished from a transient failure, which DOES back off and only dead-letters at `MAX_ATTEMPTS`.)

**LAYER3 interlock + gating (PINNED):** the janitor's belief-write step is conditional on `_graph_sim_layer3_enabled()` (`done.py:97`), NOT just on `APOLLO_LEARNER_JANITOR_ENABLED`. The other pending-setter (the shadow chain, `done_grading.py`) fires whenever `APOLLO_GRAPH_SIM_SHADOW_ENABLED` is ON — INDEPENDENT of LAYER3. A janitor enabled while LAYER3 is OFF that called `run_learner_update` would turn belief writes ON via the back door, bypassing the calibration gate the LAYER3 flag enforces. **LOCK:** with LAYER3 OFF the janitor re-runs `run_graph_simulation` only (re-persisting the shadow run idempotently — supersede-DELETE-first, so no row leak) and re-DEFERS the belief write (leaves `pending` set, records a non-terminal "layer3 disabled" outcome that is NOT counted toward `MAX_ATTEMPTS` and emits a DISTINCT log line so a misconfigured "worker on, LAYER3 off, backlog never clears" state is visible — see observability). Test: JANITOR ON + LAYER3 OFF writes ZERO `apollo_mastery_events`.

**key_risks (owned, all reproduced):**
- **[HIGH] `old_rubric` is hard-required.** `calibration.py:88,93` unconditional deref (re-verified verbatim). Mitigated by the `diagnostic_report["rubric"]` source + the dead-letter pre-flight. Tests: populated `diagnostic_report` re-runs; `None`/legacy dead-letters with NO LLM call.
- **[HIGH — make-or-break] wrong-problem re-derivation.** `done_inputs.py` MUST key `problem_payload` on `attempt.problem_id`, never `sess.current_problem_id` (`done.py:262` vs `next.py:94`). Invisible in single-problem fixtures. **Mandatory test (H1):** a session advanced PAST the pending attempt's problem still re-grades `attempt.problem_id`.
- **[HIGH] re-run is 2 LLM + Neo4j + re-commit per cycle, not a cheap fold.** The shadow is ephemeral. `MAX_ATTEMPTS=3` bounds duplicated SPEND; backoff prevents same-sweep re-claim.
- **[HIGH] WORK-session connection held across the LLM calls (textbook class).** The frozen `run_graph_simulation(db,…)` holds a connection across both LLM calls (`:203…:256`). `pool_recycle` does NOT save a held-checked-out connection. Mitigations (a)/(b)/(c) above; minutes-scale not 1.5h. **Test:** drop the connection mid-work, assert clean back-off.
- **[HIGH] claim must LEASE, not just lock.** The claim-txn releases the FOR-UPDATE lock at commit (by design), so a second drainer could re-claim the SAME row during the unlocked work window. **Fix (locked):** the claim-txn sets `next_attempt_at = now() + CLAIM_LEASE_S` so the row drops out of the predicate while work runs (mirrors `teacher_weekly.py:914`). **Test (H2):** two concurrent drainers over the SAME committed pending row → exactly one works it; the other skips.
- **[HIGH] inner re-flag interacts with the accounting txn.** `done_grading._set_pending_and_commit` (`:156-162`) and `learner_update._set_pending_and_commit` (`:65-71`) re-commit `pending=True` on re-failure in the WORK session and re-raise. The record-txn (a DIFFERENT session) RE-SELECTs the row by id (never a stale ORM object — precedent `teacher_weekly.py:1184`) and is idempotent w.r.t. that re-flag — increment is via `attempts += 1` at CLAIM (not in the record-txn), so a double-write of `pending=true` is a no-op. Test: re-fail increments `attempts` by exactly 1, flips `failed_permanently` at 3.
- **[MEDIUM] connection-lifetime discipline is BINDING** (textbook handoff). `pool_recycle=1800` is set (`session.py:94`) but insufficient for a held-checked-out connection. Short-txn shape + the `run_graph_simulation` internal commit force the claim/record sessions short; the WORK session is the bounded residual (above).
- **[MEDIUM] evidence_count crash-between-commits over-count window (critic).** On a janitor SUCCESS, `run_learner_update` commits the belief fold (incl. `_upsert_learner_state evidence_count += 1`, `persistence.py:243`) in the WORK session, but the clear-flag is in the SEPARATE record-txn. A crash BETWEEN those two commits re-surfaces the row (partial index still matches), and a re-drain re-folds (belief-idempotent via supersede DELETE-then-reinsert → same posterior) BUT `_upsert_learner_state` increments `evidence_count` AGAIN on the now-existing `apollo_learner_state` row (PK `(user, search_space, entity)`, `026:132`). **Bound: at most +1 `evidence_count` per entity per crash-between-commits; belief stays correct.** This is recoverable-not-corrupting. **Decision (locked, accept+document — `open_decisions` #1):** accept the bound and add an explicit test that drains, simulates a crash before the clear-txn, re-drains, and asserts belief-identical + `evidence_count` drift ≤ the documented bound. (Alternatives — fold the clear into `run_learner_update`'s commit, or a final no-LLM re-verify — are listed in `open_decisions` #1 for the orchestrator.)
- **[MEDIUM] thundering-herd / backlog drain.** Both setters fire on every Layer-3-window infra failure; an Aura pause (§7) or OpenAI rate-limit window flags a class at once. Bounded per-sweep `LIMIT` (small — recommend 1, see 5B3b loop pacing) + jittered backoff + the work-lease + the partial index keep the drain paced. (The loop-sleep + sweep size are 5B3b.)
- **[LOW] empty Neo4j subgraph dead-letter.** `read_node_graded_at` returning `{}` (never-stamped legacy attempt) → no `done_ts` → dead-letter (terminal), same path as `old_rubric`-missing. Test it (H3 for the helper; H1 for the dead-letter outcome).
- **[determinism, tested] original `done_ts`, never wall clock — and it is a tz-AWARE datetime.** Read from Neo4j `graded_at` (an ISO string, `store.py:559`), parsed via `datetime.fromisoformat` (preserves the UTC offset) and **asserted tz-aware** — a naive value would TypeError on the `(done_ts - last_evidence_at).days` Δt subtraction (interpreter-verified). `freeze` is PG-only (`store.py:247-253`) so the frozen graph == the original. Determinism scoped to the Δt anchor; the re-run re-calls the live LLM (findings/belief MAY differ, §3 L462-464). Test: the parsed `done_ts` equals the original stamped instant byte-for-byte; original Done (shadow+LAYER3 ON, stubbed LLM) vs a janitor re-run → IDENTICAL Δt anchor + (under the deterministic stub) identical posterior.
- **[LOW] two clocks, both correct — do NOT unify.** The claim/backoff scheduling uses wall-clock `now()` in SQL (predicate `next_attempt_at <= now()`, lease `now() + CLAIM_LEASE_S`) — this is the SCHEDULING clock (when is this row due). The belief Δt anchor uses the frozen Neo4j `graded_at` `done_ts` (NEVER `now()`). A reviewer must NOT "fix" the SQL `now()` to use `done_ts`; they are two different, correct clocks.
- **[LOW] observability.** A structured `DrainResult` per-sweep log (`claimed/succeeded/dead_lettered/retried/backlog_remaining`) mirroring the `graph_sim_calibration` extra-dict (`done_grading.py:282-289`). PLUS (critic) a WARNING per dead-letter with the reason (`diagnostic_report_missing | rubric_missing | graded_at_missing | max_attempts_exhausted`) — mirroring the upload worker's exhaustion WARNING (`teacher_weekly.py:1192`); and a DISTINCT INFO/WARNING for the LAYER3-interlock deferral (`attempt_id + "layer3_disabled, deferred, not counted"`) so a misconfigured backlog is not mistaken for a stuck drain. Backlog gauge = `COUNT WHERE pending AND NOT failed_permanently` (served by the partial index); a backlog-AGE gauge (oldest pending `created_at`) is recommended so a permanently-stuck-due-to-interlock backlog is visible.
- **[LOW] comparison-run re-persist is idempotent (critic-confirmed).** `persist_comparison_run` DELETE-firsts on `(attempt_id, comparison_version)` (`grading/persistence.py:231-236`), so the LAYER3-OFF re-defer loop (re-runs the shadow every drain) does NOT leak `apollo_graph_comparison_runs`/`_findings` rows. Test: drain N+1 of the same attempt does not multiply comparison-run rows.
- **[validation rows excluded] confirmed** — only `_PENDING_ON_ERRORS` infra failures set the flag (`done_grading.py:304-322`); 422/409 never enter the janitor.

---

### WU-5B3b — the dormant `apollo-janitor` process + opportunistic backstop (thin wiring)

**One-line:** A NEW async-native worker entrypoint that runs `while not stop_event: await drain_pending_attempts(...); await asyncio.sleep(poll)` gated on `APOLLO_LEARNER_JANITOR_ENABLED` (default OFF), plus a SIGTERM `asyncio.Event` handler and a second Procfile process, plus a bounded, POST-COMMIT opportunistic backstop in `handle_done` — all thin wrappers around the 5B3a core.

**SEAM (owns vs 5B3a):** owns the `while`-shell wrapper, the Procfile line, the flag-read, the SIGTERM handler, and the opportunistic `done.py` hook. It calls the 5B3a `drain_pending_attempts`; it adds NO drain logic.

**Scope — files NEW:**
- `apollo/learner_janitor_worker.py` (NEW) — `main()` runs `asyncio.run(_loop())`; `_loop()` is the async analogue of `run_upload_worker_loop` — a `while not stop_event` calling `await drain_pending_attempts(neo, limit=…, max_attempts=MAX_ATTEMPTS)` then `await asyncio.sleep(poll_seconds)`; early-returns (logs "janitor disabled" once + sleeps) when `APOLLO_LEARNER_JANITOR_ENABLED` is OFF. Async-native (stays on ONE asyncio loop so the per-loop engine/pool registry, `session.py:98-120`, is stable) — NOT bridged through the sync `run_async` daemon. A minimal SIGTERM `asyncio.Event` handler stops claiming new rows and finishes the in-flight attempt (a killed mid-run attempt is safe — idempotent supersede + the row stays `pending`, the work-lease expires).
  - **PRAGMA SCOPE (critic-corrected):** the `# pragma: no cover` exemption covers ONLY the bare `asyncio.run(_loop())` entrypoint line and the `while not stop_event:` shell line — NOT the loop body. The flag-off early-return, the LAYER3-interlock branch (if surfaced here), the `await drain_pending_attempts` call, the `asyncio.sleep`, and the SIGTERM-handler registration ALL carry real unit tests. (The prior draft's "mirrors `teacher_weekly.py:355`" was imprecise — that pragma is on `except KeyboardInterrupt`, and the upload loop body IS tested.) ~3 unit tests: flag-off early-return; one-sweep-then-stop drives the body once; SIGTERM sets the stop event.
  - **Loop pacing (critic):** the worker is intentionally LOW-throughput (a correctness drain, not a high-volume queue). Pin the sweep size SMALL — recommend `limit=1` (or a small constant ≤5) so a sweep does not fire `2*LIMIT` GPT-4o calls back-to-back and re-trigger the very OpenAI rate-limit that created the backlog. Pin `poll_seconds` explicitly (recommend ≥ the expected per-row LLM time so the loop self-paces). Rows process sequentially (the short-txn discipline already serializes). Document the expected steady-state drain rate; the jittered backoff spaces RE-tries, the small sweep + poll space the FIRST drain of a fresh backlog.
  - **Graceful shutdown semantics (critic):** Railway's SIGTERM→SIGKILL grace is short (~30s) and a single attempt's 2 LLM calls can exceed it. With increment-at-CLAIM, a SIGKILL'd attempt consumes a retry without a real outcome (a row could exhaust MAX_ATTEMPTS via repeated kills). The work-LEASE mitigates this: a killed attempt's `next_attempt_at` lease expires and the row re-drains; MAX_ATTEMPTS=3 is generous enough that an occasional shutdown-kill does not dead-letter a healthy row, and shutdown-interrupted attempts are logged distinctly. (The alternative — count-at-record instead of at-claim — fully decouples "claimed" from "attempted" but requires the lease anyway; the lease is the load-bearing piece either way. Surfaced as a sub-point of `open_decisions` #7's shutdown handling; recommend keep increment-at-claim + lease + distinct shutdown log.) The SIGTERM handler does cooperative cancel BETWEEN rows, never mid-row.

**Scope — files EDIT:**
- `Procfile` (EDIT, +1 line) — `apollo-janitor: python -m apollo.learner_janitor_worker`. A SECOND process, NOT a second loop bolted into the sync `worker` (the upload worker is SYNC and its `time.sleep` blocks the thread — an async drain there would starve it). Dormant unless `APOLLO_LEARNER_JANITOR_ENABLED` is set on that process. **Ops note (critic):** a Procfile entry means Railway runs a process for it; while the feature is dormant everywhere, the recommended deploy posture is to leave the `apollo-janitor` process scaled to 0 (or unscaled) until the flag is flipped, so a dormant feature does not cost a running replica. Reconcile in `docs/architecture/_overview.md` ops entrypoints.
- `apollo/handlers/done.py` (EDIT, ~6 lines, OPTIONAL — `open_decisions` #7) — the opportunistic backstop, **critic-revised to POST-COMMIT + bounded + own-session**: AFTER the student's grade is computed and committed (never before — a slow/failing drain must not delay or risk the student's own grade), gated on `APOLLO_LEARNER_JANITOR_ENABLED`, call `await asyncio.wait_for(drain_pending_attempts(neo, limit=1, max_attempts=MAX_ATTEMPTS, user_id=sess.user_id), timeout=BACKSTOP_TIMEOUT_S)` scoped to the current student's OTHER due pending attempts (the attempt being graded THIS request is not yet pending). It runs on its OWN fresh `get_async_session`s (the same short-txn discipline; NEVER the live request's `db` session, so it cannot pollute/commit the in-flight Done txn). Timeout + errors are swallowed-to-log — the backstop is a latency optimization, NEVER a correctness dependency (the worker is the correctness path). Single-flight via SKIP LOCKED + the work-lease; no second claim path.

**depends_on:** WU-5B3a.
**real_infra:** **worker-process** (the loop) + the same real-PG/Neo4j/stubbed-LLM for the flag-read + opportunistic-hook unit tests. Only the `asyncio.run` entrypoint + the `while` shell are `# pragma: no cover`.
**migration:** none (028 ships in 5B3a).
**v1-or-defer:** **worker process v1-IN (dormant); opportunistic backstop — RECOMMENDED DEFER (OPS critic) or ship POST-COMMIT+bounded+separately-controllable (`open_decisions` #7).**

**key_risks (owned):**
- **[MEDIUM] worker interleaving.** A SEPARATE `apollo-janitor` process (async-native) avoids starving the sync upload worker. Do NOT bolt the async loop into `run_upload_worker_loop`.
- **[MEDIUM] backstop on the Done hot path (critic).** If placed pre-commit, a slow/failing drain (firing precisely during the degraded windows that CREATED the backlog) blocks the student's HTTP response by up to 2 LLM-call latencies. **Fix (locked):** POST-COMMIT, own-session, `asyncio.wait_for`-bounded, swallow-to-log; or DEFER entirely (OPS recommendation). Never on the critical path of the grade.
- **[MEDIUM] loop pacing / thundering-herd at the sweep level.** Small sweep (`limit=1`/≤5) + explicit `poll_seconds` ≥ per-row LLM time; sequential rows. Low-throughput by design.
- **[LOW] graceful shutdown.** Railway SIGTERM→SIGKILL after ~30s. The `asyncio.Event` handler stops claiming between rows; a killed mid-run attempt is safe (idempotent supersede; the work-lease re-surfaces it; MAX_ATTEMPTS generous; shutdown-kills logged distinctly).
- **[LOW] dormancy cost.** The dormant process still occupies a Railway replica slot unless scaled to 0; document the "scale to 0 until the flag flips" posture.
- **[coverage] the only un-coverable lines are the `asyncio.run` entrypoint + the `while` shell** — the documented `# pragma: no cover` exemption (NOT the loop body). The flag-read, the interlock branch, and the opportunistic hook get unit tests.

---

## 4. Cross-cutting decisions (LOCKED, except where flagged)

1. **The clear-flag is written by the JANITOR's record-txn, NOT inside frozen `run_learner_update`** (DISSENT from the pre-scoping). This preserves the adjudication-#1 guardrail "every WU-5A2 test stays byte-identical green" — editing `run_learner_update` to set `pending=False` couples the frozen Layer-3 handler to the janitor lifecycle. A separate clear-txn is acceptable because re-draining a successfully-folded attempt is belief-idempotent (DELETE-then-reinsert → same posterior); the only residual is a bounded `evidence_count` +1 on a crash between the fold-commit and the clear-commit (accept+document+test — see the MEDIUM risk row). **`open_decisions_for_orchestrator` #1 — confirm location AND the evidence_count residual handling.**
2. **ONE new flag, default OFF, calibration-gated:** `APOLLO_LEARNER_JANITOR_ENABLED` (5B3b). The belief-write step is ADDITIONALLY interlocked on `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (the back-door guard). Decay/negotiation flags are inherited at re-run time via `run_learner_update`'s internal reads (`learner_update.py:49-62`).
3. **The janitor RE-RUNS from resolution** (re-builds the ephemeral shadow with `old_rubric` from `diagnostic_report`, keyed on `attempt.problem_id`, `done_ts` from Neo4j `graded_at` parsed tz-aware), clears the flag on success, dead-letters + backs off failing rows, respects the LAYER3 interlock, runs short-lived claim/record sessions with a work-lease. Re-implements no fold. Determinism scoped to the Δt anchor.
4. **Migration 028 is the ONLY migration** (5B3a): dead-letter + backoff cols + partial index. NO `graded_at` column. Testcontainers/local-Docker only; NEVER remote.
5. **A NEW named error** `LearnerUpdateUnreconstructableError(ApolloError)` makes the dead-letter path deliberate (not a caught generic crash). The dead-letter predicate = `diagnostic_report` None / `rubric` absent / `["overall"]` absent / Neo4j `graded_at` empty.
6. **NO new packages.** `.with_for_update(skip_locked=True)` (core SQLAlchemy); `asyncio`/`time`/`random`/`signal` (stdlib) cover claim + lease + loop + jittered backoff + graceful shutdown. No queue library (graphile/river/pgmq) — they'd duplicate `_claim_upload_job_async` and add a forbidden dep. `tenacity` is UNNECESSARY (backoff lives in the DB row). **`open_decisions` #4.**

---

## 5. Re-derivability table (every `run_graph_simulation` / `run_learner_update` input → its persisted source)

`run_graph_simulation(db, neo, *, attempt, sess, student_graph, problem_payload, old_rubric)` (`done_grading.py:165-174`); `run_learner_update(db, *, sess, attempt, shadow, done_ts, parser_confidence)` (`learner_update.py:74-82`).

| input | re-derivable? | persisted source (cite) | janitor note |
|---|---|---|---|
| `attempt` (`ProblemAttempt`) | YES | the claimed pending row | entry point |
| `sess` (`ApolloSession`) | YES | join `apollo_sessions` on `attempt.session_id` (`models.py:269`) | carries `user_id`/`search_space_id`/`concept_id` |
| `student_graph` (`KGGraph`) | YES (Neo4j) | `store.read_graph(attempt_id=attempt.id)` | `freeze` is PG-only (`store.py:247-253`) → frozen == original |
| `problem_payload` (raw dict) | YES | `_find_problem_payload(db, concept_id=sess.concept_id, problem_code=attempt.problem_id)` (`done.py:229-250`) | **`attempt.problem_id`, NOT `sess.current_problem_id`** (`next.py:94`) — make-or-break |
| `old_rubric` | YES (durable) | `attempt.diagnostic_report["rubric"]` (`done.py:335-339`, JSONB `models.py:275`) | dead-letter if None/legacy (`calibration.py:88,93` unconditional) |
| `done_ts` | YES (Neo4j) | `read_node_graded_at(attempt_id)` (NEW; `stamp_graded_at` `store.py:537-575`) | ISO string → `fromisoformat` (tz-aware); dead-letter if `{}`; NO PG column |
| `parser_confidence` | YES (pure) | `min_parser_confidence_of(student_graph.nodes)` (`abstention.py:70`) | operates on `read_graph` nodes |
| `shadow` (`ShadowGradeResult`) | re-BUILT | NOT loadable — re-run `run_graph_simulation` (ephemeral, `done_grading.py:91-113`) | the 2-LLM re-run; re-persist is supersede-idempotent (`grading/persistence.py:231`) |
| transcript (internal) | YES | `apollo_messages` joined by `_read_transcript` (`done_grading.py:143-153`) | internal to `run_graph_simulation` |
| decay/negotiation flags | YES (env) | read internally by `run_learner_update` (`learner_update.py:49-62`) | inherited live |

**No input is non-derivable.** The only re-built input is the ephemeral shadow (the re-run's purpose). Two inputs gate the dead-letter (`old_rubric`, `done_ts`).

---

## 6. Per-sub-unit contract tables

### WU-5B3a — janitor core
| Field | Value |
|---|---|
| Kind | claim-LEASE drain + re-run-from-resolution + dead-letter/backoff + LAYER3 interlock + clear-flag; CALLS frozen orchestration |
| Files (NEW) | `database/migrations/028_apollo_learner_janitor.sql`, `apollo/handlers/done_inputs.py`, `apollo/handlers/learner_janitor.py` |
| Files (EDIT) | `apollo/errors.py` (+`LearnerUpdateUnreconstructableError`), `apollo/knowledge_graph/store.py` (+`read_node_graded_at`), `apollo/handlers/done.py` (refactor shadow inputs to `build_rerun_inputs`) |
| Frozen public API | `drain_pending_attempts(neo,*,limit,max_attempts[,user_id])->DrainResult`; `build_rerun_inputs(db,neo,*,attempt,sess)->RerunInputs`; `KGStore.read_node_graded_at`; `LearnerUpdateUnreconstructableError` |
| Consumes | `run_graph_simulation`, `run_learner_update`, `_find_problem_payload`, `min_parser_confidence_of`, `Neo4jClient`, the supersede-idempotent fold |
| depends_on | WU-5B2 (#41), WU-5A2 |
| real_infra | **real-PG (claim/lease/clear/dead-letter/backoff + migration via H1 savepoint + H2 two-connection); real-Neo4j (`read_node_graded_at` via H3 `neo4j_client`); LLM STUBBED**; NO process |
| migration | **028** (dead-letter+backoff cols + partial index) — Testcontainers only; NO `graded_at` col |
| v1-or-defer | **v1-IN** |
| Key risks owned | `old_rubric` required; wrong-problem key (make-or-break); re-run cost; WORK-session held connection; claim-must-lease; inner-reflag accounting; evidence_count crash window; session discipline; thundering-herd; LAYER3 interlock; empty-subgraph dead-letter; Δt-anchor tz-aware determinism; two-clocks; comparison-run re-persist idempotent |

### WU-5B3b — dormant process + opportunistic backstop
| Field | Value |
|---|---|
| Kind | thin async worker loop + Procfile process + SIGTERM handler + POST-COMMIT opportunistic `done.py` hook |
| Files (NEW) | `apollo/learner_janitor_worker.py` |
| Files (EDIT) | `Procfile` (+1 process), `apollo/handlers/done.py` (~6 lines opportunistic backstop, optional/defer) |
| Frozen public API | the worker `main()` entrypoint |
| Consumes | `drain_pending_attempts` (5B3a), `APOLLO_LEARNER_JANITOR_ENABLED` |
| depends_on | WU-5B3a |
| real_infra | **worker-process** + unit tests for flag-read/interlock/opportunistic hook; only the `asyncio.run` entrypoint + `while` shell are `# pragma: no cover` |
| migration | none |
| v1-or-defer | **process v1-IN (dormant); opportunistic backstop DEFER-or-POST-COMMIT-bounded (confirm)** |
| Key risks owned | worker interleaving (separate process); backstop on Done hot path (post-commit fix); loop pacing; graceful shutdown; dormancy replica cost; the `# pragma: no cover` shell only |

---

## 7. Build order (stacked branches)

1. **WU-5B3a (CORE)** branches off `feat/apollo-kg-wu5b2-negotiation-multiplier` (#41 tip — already merged into the current branch). Migration 028 + the error + `done_inputs.py` + `read_node_graded_at` + `drain_pending_attempts` (claim-LEASE + recompute + dead-letter/backoff + LAYER3 interlock + clear-flag) + the `done.py` shadow-input refactor. Independently green at ≥95% patch coverage with the H1/H2/H3 harness composition (real-PG + real-Neo4j + stubbed-LLM). **Ships FIRST — this is the first unit.** (IF the build-unit planner confirms 5B3a XL on the three-harness cost, split into 5B3a-0 foundation → 5B3a-1 drain — `open_decisions` #6.)
2. **WU-5B3b (PROCESS)** branches off WU-5B3a. The dormant `apollo-janitor` worker + Procfile + SIGTERM handler + (optional/post-commit) opportunistic backstop. Only the `asyncio.run` entrypoint + the `while` shell are `# pragma: no cover`; the flag-read/interlock/opportunistic hook get unit tests. Lands LAST (cannot exist until the core does).

5B3a ≈ 1 orchestration module + 1 builder + 1 helper + 1 error + 1 migration + ~11 tests:
  - **H1 (`db_session` savepoint, Neo4j stubbed):** happy drain + clear; `old_rubric`-None dead-letter (no LLM); empty-subgraph dead-letter; wrong-problem re-derivation (session advanced past); backoff/`MAX_ATTEMPTS` accounting; inner-reflag accounting; LAYER3-OFF zero-events; Δt-anchor tz-aware determinism; mid-work connection-drop clean back-off; comparison-run re-persist non-duplication.
  - **H2 (raw `_pg_url` two-connection):** SKIP-LOCKED two-claimer contention over the SAME committed row (the work-lease guarantees single-flight); migration-028 chain test.
  - **H3 (`neo4j_client`):** `read_node_graded_at` returns the stamped value; returns `{}` on an unstamped subgraph.

5B3b ≈ 1 thin module + 2 edits + ~3 unit tests (flag-off early-return; one-sweep-then-stop drives the body once; SIGTERM sets the stop event) + the pragma'd `asyncio.run`/`while` shell.

**Test harness (CORRECTED — three shapes, NOT one):**
- **H1** — mirror `tests/database/test_done_layer3_route_postgres.py`'s `db_session` + LLM stubs (`main_chat_adjudicator`/`main_chat_auditor`, `:813-814`) + env-flag monkeypatch (`:858-859`); STUB `read_node_graded_at`/`read_graph` (Neo4j) since the assertion target is PG claim/clear/dead-letter/backoff state. This is the prior draft's cited harness — valid ONLY for single-claimer drain/accounting tests.
- **H2** — the raw `_pg_url` two-connection asyncpg pattern from `tests/database/test_apollo_learner_model_migration.py` (`asyncpg.connect`, committed rows, two independent connections) — REQUIRED for SKIP-LOCKED contention + claim-visibility + the migration chain. The `db_session` savepoint monkeypatch CANNOT exercise SKIP LOCKED.
- **H3** — the `neo4j_client` conftest fixture (`conftest.py:228-239`, real `neo4j:5.25`) — REQUIRED for `read_node_graded_at` (a real Cypher MATCH a mock cannot validate). The route harness's `neo=object()` + AsyncMock Neo4j is NOT real Neo4j.

---

## 8. Doc / coverage obligations

- **Patch coverage ≥95%** on changed lines via `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b2-negotiation-multiplier --fail-under=95` (the per-unit parent-branch gate every prior unit used; CI's `origin/staging` cumulative gate applies at eventual merge — parent adjudication #2c). The janitor's worker-loop coverage needs Docker regardless. The only un-coverable lines are the `asyncio.run` entrypoint + the `while` shell (`# pragma: no cover` — NOT the loop body, NOT the flag-read/interlock/hook).
- **NEVER apply migration 028 to any remote DB.** Local Testcontainers exclusively; deploying (test rehearsal then prod) is a human/CI step.
- **NO new packages** (§4.6). Confirm before adding anything.
- **Drift contract:** reconcile `docs/architecture/apollo.md` (owner doc, `owns: apollo/**`, `last_verified: 2026-06-19`) in the SAME commit each sub-unit lands — register `learner_janitor.py`, `learner_janitor_worker.py`, `done_inputs.py`, `read_node_graded_at`, `LearnerUpdateUnreconstructableError`; document the `APOLLO_LEARNER_JANITOR_ENABLED` flag + LAYER3 interlock, the re-run-from-resolution + `old_rubric`/`done_ts` sources + the `attempt.problem_id` key, the dead-letter+backoff schedule + constants + the work-lease, the short-txn discipline (incl. the WORK-session held-connection residual), the Δt-anchor tz-aware determinism rule + the two-clocks note, migration 028, and the clear-flag-in-janitor decision; bump `last_verified` to 2026-06-19. The `Procfile` edit + the "scale to 0 while dormant" posture are ops surface (`docs/architecture/_overview.md` ops entrypoints) — reconcile there too.
- **Conventional commits, NO attribution/Co-Authored-By.** Stacked DRAFT PRs, never merged.

---

## 9. Summary table

| Sub-unit | Seam (input → output) | Files | Migration | Real-infra | Key risk owned | v1? |
|---|---|---|---|---|---|---|
| **WU-5B3a** | pending rows → claim-LEASE + re-run from resolution (+`old_rubric` from `diagnostic_report`, `done_ts` from Neo4j `graded_at` tz-aware, keyed on `attempt.problem_id`) + clear-flag + dead-letter/backoff + LAYER3 interlock | `learner_janitor.py`/`done_inputs.py`/`028…sql` (NEW); `errors.py`/`store.py`/`done.py` (edit) | **028** | real-PG (H1 savepoint + H2 two-connection) + real-Neo4j (H3); LLM stubbed; no process | `old_rubric`; wrong-problem key; re-run cost; WORK-session held conn; claim-must-lease; inner-reflag; evidence_count crash window; LAYER3 interlock; Δt tz-aware determinism | **IN** |
| **WU-5B3b** | `drain_pending_attempts` → dormant async worker loop + POST-COMMIT opportunistic backstop | `learner_janitor_worker.py` (NEW); `Procfile`/`done.py` (edit) | none | worker-process (only `asyncio.run`+`while` shell `# pragma: no cover`) | worker interleaving; backstop on hot path; loop pacing; graceful shutdown; dormancy cost | **IN (dormant); backstop defer-or-post-commit** |

**The one-sentence split:** WU-5B3a is the real-PG-tested drain CORE — claim a pending attempt with `FOR UPDATE SKIP LOCKED` AND set a short work-lease in one short txn, re-assemble the re-run inputs from durable sources (keyed on `attempt.problem_id`, `old_rubric` from `diagnostic_report`, `done_ts` from Neo4j `graded_at` parsed tz-aware) via a shared `done_inputs.py`, re-run the frozen `run_graph_simulation`+`run_learner_update` in a fresh WORK session (no FOR-UPDATE lock held across the LLM calls, though that session DOES hold a connection — bounded, minutes-scale), then in a fresh record-txn re-SELECT the row and clear the flag on success or dead-letter/back-off (terminal-and-LLM-free when the inputs are unreconstructable, gated on the LAYER3 interlock for the belief write) — and WU-5B3b is the thin dormant `apollo-janitor` process + POST-COMMIT bounded opportunistic backstop that wraps that core in a `# pragma: no cover`-shell poll loop.

---

## ORCHESTRATOR ADJUDICATION NEEDED

Genuine open decisions only — an executor must NOT guess these.

1. **[CLEAR-FLAG LOCATION + evidence_count residual — the #1 seam call, DISSENT from the pre-scoping]** Clear `learner_update_pending` in the **janitor's record-txn** (RECOMMENDED — preserves the WU-5A2 byte-identity guardrail, keeps frozen `run_learner_update` untouched; safe because re-draining a folded attempt is belief-idempotent and the partial index never re-surfaces a cleared row) **vs** inside frozen `run_learner_update`'s success path (the pre-scoping's "single most important line" — couples the frozen Layer-3 handler to the janitor lifecycle, risks adjudication-#1 guardrail). The record-txn location leaves a **bounded residual**: a crash between the WORK-session fold-commit and the record-txn clear-commit re-folds idempotently in belief but increments `evidence_count` by +1 per entity (`persistence.py:243`). **Confirm: (a) clear in the janitor record-txn; and (b) the evidence_count handling — accept+document+test the +1 bound (RECOMMENDED), OR fold the clear into `run_learner_update`'s commit, OR add a final no-LLM re-verify before clearing.** If instead the clear goes inside `run_learner_update`, the WU-5A2 suite must be re-asserted byte-identical with the flag OFF.

2. **[OPPORTUNISTIC BACKSTOP — defer vs ship POST-COMMIT, and the placement that makes it safe]** The OPS critic recommends **DEFER** the in-request backstop (the dormant worker alone satisfies the drain contract). If shipped in v1, it MUST be: (a) POST-COMMIT — after the student's grade is committed, never before; (b) on its OWN `get_async_session`s, never the live request `db`; (c) `asyncio.wait_for`-bounded with a small timeout, swallowing timeout/errors to log; (d) ideally separately controllable from the worker flag so prod can run the worker without ever touching the Done hot path. **Confirm: DEFER the backstop, or ship it under these four constraints?** (This is the highest-stakes v1-scope call; the parent adjudication #2a wanted both, but it predates the hot-path latency analysis.)

3. **[5B3a INTERNAL SPLIT — 2-way default vs the 5B3a-0/5B3a-1 pre-step]** RECOMMENDED: 5B3a is ONE unit. The 3-way (migration+claim / recompute / input-builder) is REJECTED (fractures the drain state machine). BUT given the three-harness composition (H1 savepoint + H2 two-connection + H3 real-Neo4j) PLUS migration 028's chain test PLUS the `done.py` refactor regression burden, 5B3a is at the top of the size band, and the foundation pieces DO have independent test surface (contra the prior draft). **Confirm: 5B3a stays one unit, OR pre-step it as 5B3a-0 (done_inputs.py + read_node_graded_at + error + migration 028, each independently green) → 5B3a-1 (claim-lease + recompute + dead-letter/backoff)** — the orchestrator should make this call based on the executor's confirmed harness cost, not leave it to the executor.

4. **[NO NEW PACKAGE — confirm]** `.with_for_update(skip_locked=True)` (core SQLAlchemy) + `asyncio`/`time`/`random`/`signal` (stdlib) cover claim + lease + loop + jittered backoff + graceful shutdown. No queue library (graphile/river/pgmq), no `tenacity` (backoff lives in the DB row). **Confirm no package is wanted** (the standing constraint forbids new packages without confirmation). No candidate is proposed — this is a confirm-the-negative.

5. **[BACKOFF + LEASE CONSTANTS — confirm the numbers]** `MAX_ATTEMPTS=3`, `BASE_DELAY_S=300`, `BACKOFF_FACTOR=4`, `MAX_DELAY_S=3600`, `JITTER=±10%`, `CLAIM_LEASE_S=600`, plus the 5B3b loop's `poll_seconds` and sweep `limit` (recommend `limit=1`, `poll_seconds ≥` per-row LLM time). The spec gives NO cap/formula — these are 5B3's to define. `MAX_ATTEMPTS=3` (not a library's 25) bounds duplicated LLM SPEND. **Confirm the constants** (or accept as the documented default). `CLAIM_LEASE_S` is NEW vs the prior draft (the work-lease that closes the double-WORK window) — confirm it.

6. **[MIGRATION COLUMN NAMES — confirm exact spelling]** `learner_update_attempts`, `learner_update_failed_at`, `learner_update_last_error`, `learner_update_next_attempt_at`, `learner_update_failed_permanently` + index `apollo_problem_attempts_pending_idx ON (created_at) WHERE learner_update_pending AND NOT learner_update_failed_permanently`. These match the pre-scoping exactly. **Confirm the names** so the executor does not improvise. (Note the table is `apollo_problem_attempts`; the ORM model lives at `apollo/persistence/models.py`, NOT `apollo/models.py`.)

7. **[GRACEFUL-SHUTDOWN COUNTING SEMANTICS — increment-at-claim+lease vs count-at-record]** RECOMMENDED: increment `learner_update_attempts` at CLAIM (mirrors the upload worker) + the work-LEASE so a SIGKILL'd attempt's lease expires and the row re-drains; MAX_ATTEMPTS=3 generous enough that occasional shutdown-kills don't dead-letter a healthy row; shutdown-interrupted attempts logged distinctly. The alternative (count only at the record-txn, on a real outcome) fully prevents a kill from consuming a retry but still needs the lease. **Confirm: increment-at-claim + lease + distinct shutdown log (RECOMMENDED), or count-at-record.** (Low-stakes but should not be left to executor guess since it changes the MAX_ATTEMPTS exhaustion semantics under repeated kills.)

**Resolved by re-verification (NOT open — recorded so the orchestrator does not re-litigate):**
- **`pool_recycle` is ALREADY set** (`session.py:94`, `=1800` + `pool_pre_ping` + `pool_size=10`). The pre-scoping's "no pool_recycle" is STALE. No "add pool_recycle" task — cite it as necessary-but-insufficient. The binding cure is the short-txn discipline; the WORK-session held-connection residual is the bounded, accepted exception (minutes-scale, refreshed by the internal commit, recovered by `pool_pre_ping` at the next checkout).
- **`persist_comparison_run` is supersede-idempotent** (DELETE-first on `(attempt_id, comparison_version)`, `grading/persistence.py:231-236`) — the LAYER3-OFF re-defer loop does NOT leak comparison-run/findings rows. (The FEASIBILITY critic flagged this as a possible append-only leak; verified NOT append-only.)
- **`old_rubric` unconditional deref** (`calibration.py:88,93`), **supersede reads the event log not LearnerState.belief** (`persistence.py:160-183`), **no PG `graded_at` column** (`apollo/persistence/models.py:265-285`), **the `next.py:94` vs `attempt.problem_id` wrong-problem bug**, the **claim/record idioms** (`teacher_weekly.py:881-935`/`:1176-1210` incl. the lease at `:914` and `session.get()` re-fetch at `:1184`), and the **backoff math** (interpreter-verified 300/1200/3600) — all RE-VERIFIED this pass and TRUE.
