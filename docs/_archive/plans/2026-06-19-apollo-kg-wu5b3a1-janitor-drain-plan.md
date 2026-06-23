# WU-5B3a-1 — `drain_pending_attempts` retry-janitor DRAIN state machine (implementation plan)

**Date:** 2026-06-19
**Unit:** WU-5B3a-1 (the drain state machine half of WU-5B3a; the foundation half WU-5B3a-0 is already merged).
**Branch:** `feat/apollo-kg-wu5b3a1-janitor-drain` (already checked out; do NOT create/switch branches, do NOT push, do NOT open PRs).
**diff-cover baseline:** `feat/apollo-kg-wu5b3a0-janitor-foundation`.
**Authoritative design:** `docs/superpowers/plans/2026-06-19-apollo-kg-wu5b3-split-proposal.md` (WU-5B3a section + ORCHESTRATOR ADJUDICATION) and `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §3 / §6 / §7.

> TDD plan only. No code is written here. Write REAL tests FIRST; mock the LLM deterministically; never apply migration 028 to any remote DB.

---

## 0. What 5B3a-0 already shipped (do NOT re-create)

Verified on disk on this branch (2026-06-19) — these are FROZEN inputs, not work for this unit:

| Artifact | Location | Status |
|---|---|---|
| Migration 028 (5 dead-letter/backoff cols + partial index `apollo_problem_attempts_pending_idx`) | `database/migrations/028_apollo_learner_janitor.sql` | DONE — idempotent, `IF NOT EXISTS` guards, rollback block present |
| ORM mapping of the 5 cols on `ProblemAttempt` | `apollo/persistence/models.py:283-295` | DONE — `learner_update_attempts` (INT, server_default 0), `learner_update_failed_at` / `learner_update_last_error` / `learner_update_next_attempt_at` (nullable), `learner_update_failed_permanently` (BOOL, server_default false) |
| `LearnerUpdateUnreconstructableError(ApolloError)` + closed reason set | `apollo/errors.py:169-195` | DONE — `__init__(*, attempt_id, reason)`; reasons `diagnostic_report_missing` / `rubric_missing` / `graded_at_missing` |
| `KGStore.read_node_graded_at(*, attempt_id) -> dict[str,str]` | `apollo/knowledge_graph/store.py:497-523` | DONE — None-omit, `{}` on empty subgraph |
| `build_rerun_inputs(db, neo, *, attempt, sess) -> RerunInputs` (+ relocated `_find_problem_payload`) | `apollo/handlers/done_inputs.py` | DONE — keys on `attempt.problem_id`; raises `LearnerUpdateUnreconstructableError` in pre-flight; returns frozen `RerunInputs(problem_payload, old_rubric, student_graph, parser_confidence, graded_at_iso)` |
| Migration-028 chain + ORM round-trip tests | `tests/database/test_apollo_learner_janitor_migration.py` | GREEN |
| `build_rerun_inputs` unit tests | `apollo/handlers/tests/test_build_rerun_inputs.py` | GREEN |

**Consequence:** this unit (5B3a-1) adds exactly ONE source file — `apollo/handlers/learner_janitor.py` — plus its tests, plus the owner-doc reconcile. It does NOT edit migration 028, the ORM, the error class, `read_node_graded_at`, or `build_rerun_inputs`.

## 1. Goal + the one new file

**Goal:** A short-transaction `drain_pending_attempts` that claims one due `learner_update_pending` attempt with `FOR UPDATE SKIP LOCKED` + a work-lease (one committed txn), re-runs the frozen `run_graph_simulation` + `run_learner_update` from durable state in a separate WORK session, and in a third record-txn clears the flag on success / dead-letters / backs off — gated on the LAYER3 interlock for the belief write. It re-implements no belief fold; it ORCHESTRATES frozen primitives.

**Files this unit creates/edits:**

| File | Kind | What |
|---|---|---|
| `apollo/handlers/learner_janitor.py` | NEW (~220 lines, < 800) | `drain_pending_attempts` + `DrainResult` dataclass + the 6 named module constants + the backoff helper |
| `apollo/handlers/tests/test_learner_janitor.py` | NEW | H1 single-claimer drain/accounting/dead-letter/backoff/LAYER3/idempotency/connection-drop tests |
| `tests/database/test_learner_janitor_contention.py` | NEW | H2 two-connection SKIP-LOCKED contention + claim-visibility |
| `tests/database/test_learner_janitor_full_drain.py` | NEW | H3 real-Neo4j full-drain success (Testcontainers `neo4j:5.25`) |
| `docs/architecture/apollo.md` | EDIT | drift reconcile (interfaces, data-flow, constants, `last_verified: 2026-06-19`) |

Three test files are split by harness shape so the Testcontainers-`neo4j_client` (defined in `tests/conftest.py`) is visible to the H2/H3 tests under `tests/database/`, and NOT shadowed by the live-Aura `neo4j_client` defined in `apollo/conftest.py` (which `skip`s when `NEO4J_URI` is unset). The H1 tests under `apollo/handlers/tests/` STUB Neo4j entirely (no fixture needed), so the live-Aura shadow is harmless there.
## 2. Public signatures (the frozen API this unit adds)

```python
# apollo/handlers/learner_janitor.py

from dataclasses import dataclass

@dataclass(frozen=True)
class DrainResult:
    """Immutable per-sweep summary (observability). Counts are over the rows
    this sweep CLAIMED. backlog_remaining is a post-sweep COUNT of still-pending,
    not-dead rows (served by the partial index)."""
    claimed: int
    succeeded: int
    dead_lettered: int
    retried: int            # transient failure -> backoff (not terminal)
    deferred: int           # LAYER3 interlock OFF -> belief write skipped, row left pending
    backlog_remaining: int


async def drain_pending_attempts(
    neo,                    # apollo.persistence.neo4j_client.Neo4jClient
    *,
    limit: int = 1,
    max_attempts: int = MAX_ATTEMPTS,
    user_id: str | None = None,   # scope to one student's due rows (WU-5B3b backstop); None = global
) -> DrainResult:
    ...
```

- `neo` is positional (mirrors `run_graph_simulation(db, neo, ...)` and the live `handle_done(db, neo, ...)` call shape). The function opens its OWN sessions via `get_async_session()` — it does NOT take a `db` argument (the short-txn discipline forbids sharing a caller session across the claim/work/record boundaries).
- `user_id` is an optional scope filter applied to the claim predicate (`ApolloSession.user_id == user_id` via a join on `attempt.session_id`). The 5B3b opportunistic backstop will pass it; the worker loop passes `None`. This unit ships it and tests both paths.
- Return type is the frozen `DrainResult`; callers (5B3b) log it.

**Backward-compat:** this is a NEW module — no existing signature changes. No frozen file is edited.

## 3. Module constants (named, env-overridable)

Named module-level constants, env-overridable in the codebase's idiom (read via a tiny `_int_env(name, default)` / `_float_env` helper at module load, mirroring how the rest of the codebase reads env, e.g. `learner_update._learner_decay_enabled`). ORCHESTRATOR-ADJUDICATED values (split-proposal §3 + adjudication #5) — do NOT re-derive:

| Constant | Value | Env var | Rationale |
|---|---|---|---|
| `MAX_ATTEMPTS` | `3` | `APOLLO_JANITOR_MAX_ATTEMPTS` | each retry = 2 GPT-4o calls + comparison-run re-commit; bounds duplicated SPEND. Mirrors the upload worker's 3-attempt exhaustion. |
| `BASE_DELAY_S` | `300` | `APOLLO_JANITOR_BASE_DELAY_S` | first backoff (5 min). |
| `BACKOFF_FACTOR` | `4` | `APOLLO_JANITOR_BACKOFF_FACTOR` | aggressive spacing on an expensive job. |
| `MAX_DELAY_S` | `3600` | `APOLLO_JANITOR_MAX_DELAY_S` | cap (1 h). Schedule: 300 / 1200 / 3600. |
| `JITTER` | `0.10` (±10%) | `APOLLO_JANITOR_JITTER` | thundering-herd defense; computed in Python `random.uniform` so it is monkeypatch-deterministic. |
| `CLAIM_LEASE_S` | `600` | `APOLLO_JANITOR_CLAIM_LEASE_S` | the work-lease written at claim (10 min) — drops the claimed row out of the claim predicate while the 2-LLM work runs, preventing a second drainer from re-claiming the SAME row mid-work. Mirrors `teacher_weekly.py:914`. |

**Backoff formula** (computed in the record-txn, Python — NOT SQL):
```
delay = min(MAX_DELAY_S, BASE_DELAY_S * BACKOFF_FACTOR ** (attempts - 1)) * (1 + uniform(-JITTER, +JITTER))
next_attempt_at = now() + timedelta(seconds=delay)
```
where `attempts = learner_update_attempts` (already incremented at CLAIM). Interpreter-verified centers: `300·4^0=300`, `300·4^1=1200`, `300·4^2=3600` (capped). Extract the delay math into a pure helper `_backoff_delay_s(attempts: int, *, jitter_fn=random.uniform) -> float` so tests pin `jitter_fn` to a deterministic value (e.g. `lambda lo, hi: 0.0` for the midpoint) and assert the schedule exactly.

## 4. The drain state machine (three-session contract)

Mirror the upload-worker idiom EXACTLY (split-proposal RECON facts; verified at `knowledge/teacher_weekly.py:881-935` claim / `:1176-1210` record). Three short-lived sessions, each its own `async with get_async_session()`.

### Phase A — CLAIM-txn (short, commits immediately; mirrors `_claim_upload_job_async`)
```python
async with get_async_session() as claim_db:
    now = _utc_now()                       # module helper: datetime.now(UTC)
    stmt = (
        select(ProblemAttempt)
        .where(ProblemAttempt.learner_update_pending.is_(True))
        .where(ProblemAttempt.learner_update_failed_permanently.is_(False))
        .where(or_(
            ProblemAttempt.learner_update_next_attempt_at.is_(None),
            ProblemAttempt.learner_update_next_attempt_at <= now,
        ))
        .order_by(ProblemAttempt.created_at.asc())      # uses 028 partial index
        .limit(limit)
        .with_for_update(skip_locked=True)              # core SQLAlchemy — NO new package
    )
    if user_id is not None:                # join apollo_sessions for the scope filter
        stmt = stmt.join(ApolloSession, ApolloSession.id == ProblemAttempt.session_id) \
                   .where(ApolloSession.user_id == user_id)
    rows = (await claim_db.execute(stmt)).scalars().all()
    for attempt in rows:
        attempt.learner_update_attempts = (attempt.learner_update_attempts or 0) + 1   # bump AT CLAIM
        attempt.learner_update_next_attempt_at = now + timedelta(seconds=CLAIM_LEASE_S) # WORK-LEASE
    await claim_db.commit()                # releases the FOR-UPDATE lock + the connection
    # capture immutable claim snapshots (id, session_id, attempts-after-claim) BEFORE
    # the session closes; the work/record phases re-SELECT by id, never reuse this object.
```
Capture each claimed row as an immutable snapshot. The split's immutability rule: never mutate/reuse the claim-session ORM object downstream — re-SELECT by id in the record-txn.

### Phase B — WORK session (a fresh `get_async_session` per claimed attempt; holds a connection across the 2 LLM calls — bounded, accepted)
```python
async with get_async_session() as work_db:
    attempt = await work_db.get(ProblemAttempt, attempt_id)          # fresh ORM object
    sess = await work_db.get(ApolloSession, attempt.session_id)
    try:
        rerun = await build_rerun_inputs(work_db, neo, attempt=attempt, sess=sess)  # may raise LearnerUpdateUnreconstructableError (terminal, NO LLM)
        shadow = await run_graph_simulation(                              # 2 LLM calls; commits internally
            work_db, neo, attempt=attempt, sess=sess,
            student_graph=rerun.student_graph,
            problem_payload=rerun.problem_payload,
            old_rubric=rerun.old_rubric,
        )
        outcome = "deferred"                                            # default when LAYER3 off
        if shadow is not None and _graph_sim_layer3_enabled():           # LAYER3 INTERLOCK
            done_ts = datetime.fromisoformat(rerun.graded_at_iso)        # tz-aware; never now()
            if done_ts.tzinfo is None:
                raise LearnerUpdateUnreconstructableError(attempt_id=attempt_id, reason="graded_at_missing")
            await run_learner_update(                                    # belief fold + commit
                work_db, sess=sess, attempt=attempt,
                shadow=shadow, done_ts=done_ts,
                parser_confidence=rerun.parser_confidence,
            )
            outcome = "success"
    except LearnerUpdateUnreconstructableError as e:
        outcome, reason = "dead_letter", e.reason
    except Exception as e:
        outcome, error = "retry_or_dead", str(e)                        # transient
```
Notes (LOCKED by adjudication):
- The CLAIM-txn's FOR-UPDATE lock is NEVER held across the LLM calls (released at the claim-commit). The WORK session DOES hold a connection across the LLM window (the frozen `run_graph_simulation` opens its txn before the LLM calls and commits at `done_grading.py:256`) — bounded, minutes-scale, recovered by `pool_pre_ping` at the next checkout. Do NOT claim "no held connection."
- `done_ts` is parsed from `rerun.graded_at_iso` (the FROZEN Neo4j `graded_at`), NEVER `now()`. A naive parse → terminal dead-letter (`graded_at_missing`) — symmetry with the empty-subgraph case (`datetime.fromisoformat` on `stamp_graded_at`'s `.isoformat()` output is tz-aware in practice; the guard is defensive).
- The belief write is gated on `_graph_sim_layer3_enabled()` (imported from `apollo.handlers.done`), NOT on any janitor flag — the back-door guard (split §3 LAYER3 interlock). With LAYER3 OFF, `run_graph_simulation` still runs (re-persists the shadow idempotently via `persist_comparison_run`'s DELETE-first), but `run_learner_update` is NOT called → `outcome="deferred"`, the row is LEFT pending and the deferral does NOT consume a retry (Phase C undoes the at-claim bump).
- `_set_pending_and_commit` inside the frozen callees may re-commit `pending=True` in `work_db` on inner failure; the record-txn is idempotent w.r.t. that re-flag.

### Phase C — RECORD-txn (short; RE-SELECT by id; mirrors `_handle_job_failure_async`)
```python
async with get_async_session() as rec_db:
    attempt = await rec_db.get(ProblemAttempt, attempt_id)   # NEVER the work-session object
    now = _utc_now()
    if outcome == "success":
        attempt.learner_update_pending = False               # cleared HERE, not in frozen run_learner_update
    elif outcome == "deferred":
        # LAYER3 OFF: a configuration deferral is NOT a failed attempt -> undo the
        # at-claim retry consumption and re-arm next_attempt_at so the row is due
        # again (not held by the full CLAIM_LEASE). Stays pending; DISTINCT log line.
        attempt.learner_update_attempts = max(0, (attempt.learner_update_attempts or 1) - 1)
        attempt.learner_update_next_attempt_at = None        # immediately re-due; worker pacing throttles
    elif outcome == "dead_letter":
        attempt.learner_update_failed_permanently = True
        attempt.learner_update_failed_at = now
        attempt.learner_update_last_error = reason           # the unreconstructable reason
    else:  # transient "retry_or_dead"
        if (attempt.learner_update_attempts or 0) >= max_attempts:
            attempt.learner_update_failed_permanently = True
            attempt.learner_update_failed_at = now
            attempt.learner_update_last_error = _truncate(error)
        else:
            attempt.learner_update_next_attempt_at = now + timedelta(seconds=_backoff_delay_s(attempt.learner_update_attempts))
            attempt.learner_update_last_error = _truncate(error)
    await rec_db.commit()
```
- The clear-flag lives HERE (adjudication #1, RECOMMENDED) — NOT inside frozen `run_learner_update` — to preserve the WU-5A2 byte-identity guardrail.
- `attempts` is the at-claim value (Phase A already bumped it). A killed-mid-work attempt (no record-txn) leaves the bumped `attempts` + the CLAIM_LEASE; the lease expires and the row re-surfaces — the kill consumes a retry (adjudication #7: increment-at-claim + lease).
- `_truncate` caps `learner_update_last_error` length; re-implement a tiny local 2-line truncate (do NOT import the private `teacher_weekly._truncate_error` across domains).

### Observability (split §3 — LOW risk, but emit)
- One `_LOG.info("learner_janitor_drain", extra={claimed, succeeded, dead_lettered, retried, deferred, backlog_remaining})` per sweep.
- A `_LOG.warning` per dead-letter with the reason (`diagnostic_report_missing|rubric_missing|graded_at_missing|max_attempts_exhausted`).
- A DISTINCT `_LOG.warning("learner_janitor_layer3_deferred", extra={attempt_id})` for the interlock deferral so a "worker on, LAYER3 off, backlog never clears" misconfig is visible.
## 5. Frozen primitives this unit ORCHESTRATES (no reimplementation)

DO NOT modify any of these. The janitor imports and calls them:

| Primitive | Source | Contract the janitor relies on |
|---|---|---|
| `build_rerun_inputs(db, neo, *, attempt, sess) -> RerunInputs` | `apollo/handlers/done_inputs.py` | re-assembles inputs from durable state; raises `LearnerUpdateUnreconstructableError` (terminal) in pre-flight; returns `graded_at_iso` (raw Neo4j ISO string) — the janitor parses tz-aware |
| `run_graph_simulation(db, neo, *, attempt, sess, student_graph, problem_payload, old_rubric) -> ShadowGradeResult \| None` | `apollo/handlers/done_grading.py:165` | 2 LLM calls, commits the comparison run internally at `:256`; on infra failure sets `pending=True` + re-raises; `persist_comparison_run` is supersede-idempotent (DELETE-first) so re-runs don't leak rows |
| `run_learner_update(db, *, sess, attempt, shadow, done_ts, parser_confidence) -> LearnerUpdateResult \| None` | `apollo/handlers/learner_update.py:74` | the belief fold + the single all-or-nothing commit; supersede-idempotent in belief (event-log recompute base, `persistence.py:151-183`); does NOT clear `pending` (the janitor clears it) |
| `_graph_sim_layer3_enabled() -> bool` | `apollo/handlers/done.py:99` | the LAYER3 interlock gate (reads `APOLLO_GRAPH_SIM_LAYER3_ENABLED`) — import and call; do NOT re-define a janitor copy |
| `get_async_session()` | `database/session.py:141` | the per-phase short session; `pool_pre_ping=True` + `pool_recycle=1800` already set (`:89,94`) |
| `ProblemAttempt` / `ApolloSession` ORM | `apollo/persistence/models.py` | the claim/work/record targets + the `user_id` scope join |

**Idempotency the janitor leans on (do NOT try to make exactly-once):**
- belief: `run_learner_update` recomputes the base from the latest DIFFERENT-attempt `MasteryEvent.posterior_belief` (never `LearnerState.belief`) + DELETEs this attempt's mastery_events first — so a re-drain of the SAME attempt yields the IDENTICAL posterior, no double-count, no `UNIQUE NULLS NOT DISTINCT(attempt_id,entity_id,event_kind)` collision.
- `evidence_count` residual: a crash between the WORK-session fold-commit and the record-txn clear can re-increment `evidence_count` by +1/entity on the next drain (belief stays correct). DOCUMENT + TEST the +1 bound; do NOT attempt exactly-once.

## 6. Test harness shapes (H1 / H2 / H3) — exact fixtures

| Shape | Fixture(s) | Where (so the right `neo4j_client` resolves) | Used for |
|---|---|---|---|
| **H1** | `db_session` (savepoint, `tests/conftest.py:162`, re-exported in `apollo/conftest.py`); Neo4j STUBBED via `patch(...KGStore.read_graph/read_node_graded_at...)`; LLM STUBBED via `patch("apollo.handlers.done_grading.main_chat_adjudicator"/"...main_chat_auditor")` | `apollo/handlers/tests/test_learner_janitor.py` | single-claimer drain success/clear, dead-letter, backoff/MAX_ATTEMPTS, LAYER3-OFF defer, supersede idempotency, inner-reflag accounting, connection-drop |
| **H2** | the raw two-connection asyncpg pattern from `tests/database/test_apollo_learner_model_migration.py` (`_pg_url` → `asyncpg.connect`, a session-scoped migrated DSN, COMMITTED rows on connection A, a second independent connection B) | `tests/database/test_learner_janitor_contention.py` | SKIP-LOCKED two-claimer contention + claim-commits-visible (the load-bearing concurrency proof) |
| **H3** | the Testcontainers `neo4j_client` (`tests/conftest.py:228`, real `neo4j:5.25`) + `db_session` + LLM STUBBED | `tests/database/test_learner_janitor_full_drain.py` | one real full drain that actually reads the frozen graph from Neo4j (`build_rerun_inputs` + `run_graph_simulation` real Neo4j reads) |

**Why H1 cannot do contention (binding):** `db_session` is a single NullPool connection inside an outer transaction that is NEVER committed (`join_transaction_mode="create_savepoint"`, teardown rolls back). It cannot (a) make a claimed-and-committed row visible to a genuinely independent connection, nor (b) demonstrate a row lock SKIPPED by a second concurrent committed transaction. SKIP-LOCKED is only observable with two real committed connections → H2.

**Why H3 lives under `tests/database/`:** `apollo/conftest.py` defines a `neo4j_client` that requires a LIVE Aura connection and `skip`s when `NEO4J_URI` is unset — it SHADOWS the Testcontainers `neo4j_client` for any test under `apollo/`. The H3 full-drain test must bind to the `neo4j:5.25` Testcontainers fixture, so it lives under `tests/database/` where `tests/conftest.py`'s fixture is the one in scope (green-not-skipped under Docker).

**REAL-INFRA GATE:** the H2/H3 files are under `tests/database/*`, so the changed-tests gate triggers — `pytest tests/database -v` must be GREEN-not-skipped (Docker up), and the H3 test spins a real Neo4j container.

### Reusable H1 stub helpers (factor into the test module, mirroring `test_done_layer3_route_postgres.py:796-822`)
- `_adjudicator_stub(_request) -> {node_id: canon_key}` and `_auditor_stub(_request) -> {"spans": {}}` — deterministic, no network.
- `_seed_pending_attempt(db, *, sid, cid, problem_code, report=_good_report(), ...)` — seed a course (via `seed_course` from `apollo/subjects/tests/_curriculum_fixtures.py`), a `REPORT`-phase session, a `graded` attempt with `learner_update_pending=True`, and a `Message` row for the transcript. Returns `(sess, attempt)`.
- `_neo_stubs(attempt_id, *, graph=None, graded_at=None)` — patch `KGStore.read_graph` + `KGStore.read_node_graded_at` + `done_grading.write_resolution` + `done_turn_order.KGStore.read_node_created_at` (all AsyncMock) so the H1 drain runs the full chain without Neo4j. Patch targets must be the SAME class object reached via `done_inputs`/`done_grading` (the route test confirms `KGStore` is one class).
- A pinned `jitter_fn` (`lambda lo, hi: 0.0`) injected into `_backoff_delay_s` so backoff deltas are exact.
## 7. Test list (TDD order — names + assertions + mocking)

Write these FIRST, RED before GREEN. No `skip`/`xfail`, no assert-nothing. `pytestmark = pytest.mark.integration` on all three files (they require Docker). All LLM mocked deterministically (no live API). TDD order = pure helper → H1 happy/clear → dead-letters → backoff/cap → LAYER3 → idempotency → connection-drop → H2 contention → H3 full drain.

### Pure unit (no DB) — in `test_learner_janitor.py`

1. **`test_backoff_delay_schedule_is_300_1200_3600`** — call `_backoff_delay_s(1/2/3, jitter_fn=lambda lo,hi: 0.0)`; assert `== 300.0 / 1200.0 / 3600.0`. Assert `_backoff_delay_s(4, ...) == 3600.0` (cap holds). Asserts the PINNED schedule + the `MAX_DELAY_S` clamp.
2. **`test_backoff_delay_jitter_band`** — with `jitter_fn=random.uniform` and a seeded `random`, assert `270.0 <= _backoff_delay_s(1) <= 330.0` (±10% band around 300). Asserts jitter is bounded and applied.

### H1 — `apollo/handlers/tests/test_learner_janitor.py` (db_session; Neo4j + LLM stubbed)

3. **`test_drain_success_clears_pending`** (MANDATORY: single-claimer drain success) — seed ONE due pending attempt (good report, stubbed graph + graded_at, LAYER3 ON via `monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED","1")`, SHADOW ON). Call `drain_pending_attempts(neo_stub, limit=1)`. Assert: re-SELECT shows `learner_update_pending is False`, `learner_update_failed_permanently is False`; `DrainResult(claimed=1, succeeded=1, dead_lettered=0, retried=0, deferred=0)`; `backlog_remaining == 0`. Assert `run_graph_simulation` + `run_learner_update` reached (spy the LLM stubs were awaited; or assert a `MasteryEvent`/`LearnerState` row exists for the entity).
4. **`test_drain_increments_attempts_at_claim`** — seed a pending attempt; patch `run_graph_simulation` to raise a transient `RuntimeError("boom")`. Drain. Assert the re-SELECTed `learner_update_attempts == 1` (bumped at claim even though work failed) and the row is still pending with a `next_attempt_at` set.
5. **`test_drain_dead_letters_on_diagnostic_report_none`** (MANDATORY: unreconstructable → IMMEDIATE dead-letter, no retry) — seed a pending attempt with `diagnostic_report=None`. Drain. Assert `learner_update_failed_permanently is True`, `learner_update_failed_at is not None`, `learner_update_last_error == "diagnostic_report_missing"`, `learner_update_next_attempt_at` NOT advanced as a backoff (terminal). Assert the LLM stubs were NOT awaited (no LLM on the dead-letter path). `DrainResult(dead_lettered=1)`.
6. **`test_drain_dead_letters_on_rubric_missing`** (MANDATORY) — report present but `"rubric"` absent / lacks `"overall"`. Same terminal assertions; `last_error == "rubric_missing"`; no LLM.
7. **`test_drain_dead_letters_on_graded_at_missing`** (MANDATORY: vanished/empty subgraph) — good report, but `read_node_graded_at` stub returns `{}`. Assert terminal `failed_permanently`, `last_error == "graded_at_missing"`, no LLM call.
8. **`test_drain_dead_letters_on_vanished_problem`** (MANDATORY: vanished-problem → dead-letter, NOT crash-loop) — seed the attempt, then ensure `build_rerun_inputs`'s `_find_problem_payload` raises (no matching `ConceptProblem` row: seed the attempt with a `problem_id` that has no problem row, or delete it). `scalar_one()` raises `NoResultFound`; the janitor MUST catch it as a terminal dead-letter (NOT propagate as a transient crash that re-surfaces forever). Assert `failed_permanently is True` and a recorded `last_error`. (Per 5B3a-0 heads-up #7b: treat `NoResultFound` as `LearnerUpdateUnreconstructableError`-class terminal. If `build_rerun_inputs` does NOT already wrap it, the janitor's `except` must classify `NoResultFound` as terminal — assert this behavior here.)
9. **`test_drain_backoff_schedule_on_repeated_transient_failure`** (MANDATORY: backoff deltas 300/1200/3600) — patch `run_graph_simulation` to raise a transient error; pin `jitter_fn` to midpoint. Drain THREE times against the SAME row (resetting `next_attempt_at <= now` between drains so it re-claims). Assert: after drain 1 `attempts==1` and `next_attempt_at ≈ now+300s`; after drain 2 `attempts==2` and `≈ now+1200s`; after drain 3 `attempts==3` → `failed_permanently is True` (the cap path, NOT a 3600 backoff — `attempts >= MAX_ATTEMPTS`). Use a tolerance window (±2s) on the deltas because `now()` differs per drain.
10. **`test_drain_max_attempts_dead_letters`** (MANDATORY: MAX_ATTEMPTS=3 → failed_permanently) — seed a pending attempt with `learner_update_attempts=2` pre-set; patch the work to raise transient. Drain ONCE → claim bumps to 3 → `attempts >= max_attempts` → assert `failed_permanently is True`, `failed_at` set, `last_error` set, and the row no longer appears in a subsequent claim (drained again → `claimed==0`).
11. **`test_drain_layer3_off_defers_without_belief_write`** (MANDATORY: LAYER3-OFF → no belief write but attempt recorded) — LAYER3 flag OFF (`APOLLO_GRAPH_SIM_LAYER3_ENABLED` unset), SHADOW ON. Drain a due pending attempt. Assert: ZERO `apollo_mastery_events` rows for the attempt; `run_learner_update` was NOT called (spy/patch it and assert `not awaited`); the row is STILL `learner_update_pending is True`; `learner_update_failed_permanently is False`; the at-claim attempts bump was UNDONE (`attempts == 0`); `learner_update_next_attempt_at is None` (re-armed). `DrainResult(deferred=1, succeeded=0)`. Assert the DISTINCT `learner_janitor_layer3_deferred` log emitted (caplog).
12. **`test_drain_supersede_idempotent_same_attempt_twice`** (MANDATORY: supersede idempotency + evidence_count +1 bound) — LAYER3 ON. Drain a due attempt to success; capture `LearnerState.belief` and `evidence_count` (call it `c1`). Force the SAME attempt pending again (`learner_update_pending=True`, `next_attempt_at=None`) WITHOUT clearing the prior fold — simulating the crash-between-commits window. Drain again. Assert: `belief` is byte-identical (the recompute base is the prior-attempt event log, not this attempt's); NO duplicate `mastery_events` rows for `(attempt_id, entity_id, event_kind)` (count unchanged — supersede DELETE-then-reinsert); no `UNIQUE` collision raised; `evidence_count` drifted by AT MOST +1 (`c2 - c1 <= 1`). Documents the accepted +1 bound.
13. **`test_drain_inner_reflag_accounting_is_idempotent`** — patch `run_graph_simulation` so that BEFORE raising it calls the real `_set_pending_and_commit` (re-commits `pending=True` in the work session). Drain. Assert the record-txn still computes the correct outcome (transient → backoff), `attempts` incremented by exactly 1 (not 2), and the double-write of `pending=True` is a no-op. Asserts the record-txn re-SELECTs by id and is idempotent w.r.t. the inner re-flag.
14. **`test_drain_connection_drop_mid_work_backs_off_cleanly`** (MANDATORY: connection-drop mid-work → clean back-off) — patch `run_graph_simulation` to raise an asyncpg-class connection error (`ConnectionDoesNotExistError` or a generic `OSError`) AFTER the LLM stub returns (simulate the textbook drop). Drain. Assert: no crash escapes `drain_pending_attempts`; the record-txn (a FRESH session) commits a clean backoff (`next_attempt_at` set, row still pending, not corrupted); the row re-surfaces on the next due drain. Asserts the WORK-session held-connection failure is recovered by the short-txn discipline.
15. **`test_drain_empty_backlog_is_noop`** — no pending rows. Drain. Assert `DrainResult(claimed=0, ...)`, no LLM call, `backlog_remaining==0`.
16. **`test_drain_user_id_scope_filters_other_students`** — seed TWO due pending attempts under DIFFERENT `user_id`s. Drain with `user_id=<student A>`, `limit=5`. Assert only A's attempt was claimed/worked (B still pending, `attempts==0`). Asserts the scope join.
17. **`test_drain_skips_already_dead_lettered_rows`** — seed a pending row with `learner_update_failed_permanently=True`. Drain. Assert `claimed==0` (the partial-index predicate excludes dead rows), no LLM.
18. **`test_drain_skips_future_next_attempt_at`** — seed a pending row with `learner_update_next_attempt_at = now + 1h`. Drain. Assert `claimed==0` (the runtime `<= now()` filter), then set it to past and drain → `claimed==1`. Asserts the due-time filter.

### H2 — `tests/database/test_learner_janitor_contention.py` (two committed connections; LLM + Neo4j stubbed at the drain level)

For H2, the two drainers must run against the migrated, COMMITTED DB. Because `drain_pending_attempts` opens its own `get_async_session()` (per-loop engine), the cleanest contention proof patches the WORK + RECORD phases to no-ops and asserts ONLY the CLAIM behavior on committed rows — i.e. drive the claim with two genuinely concurrent drains and assert exactly one claims the row. Concretely: seed + COMMIT one due pending attempt; run two `drain_pending_attempts` coroutines concurrently (`asyncio.gather`) with `run_graph_simulation`/`run_learner_update` patched to record "which drain worked which attempt" and return success. Assert EXACTLY ONE drain reports `claimed==1` and the other `claimed==0`.

19. **`test_two_concurrent_drainers_one_claims_via_skip_locked`** (MANDATORY: H2 two-claimer SKIP-LOCKED — THE load-bearing concurrency proof) — seed+commit one due pending attempt; `await asyncio.gather(drain(...), drain(...))`. Assert the two `DrainResult.claimed` values are `{1, 0}` (exactly one worked it), and the worked attempt cleared/succeeded exactly once (no double belief write). Patch the work-phase callees so no real LLM/Neo4j is needed; the assertion target is the claim lock + lease, on COMMITTED rows.
20. **`test_claim_lease_prevents_reclaim_during_work`** — drainer 1 claims (sets the CLAIM_LEASE) and is paused mid-work (an `asyncio.Event` the patched work awaits); drainer 2 drains while drainer 1 holds the lease. Assert drainer 2 finds `claimed==0` (the leased row's `next_attempt_at` is in the future). Release drainer 1. Asserts the work-lease closes the double-WORK window.
21. **`test_committed_claim_is_visible_to_independent_connection`** — connection A (raw asyncpg) seeds+commits a pending row; a `drain_pending_attempts` then claims it and commits; a fresh asyncpg connection B `SELECT`s the row and sees `learner_update_attempts == 1` + `next_attempt_at` in the future. Asserts claim-commit visibility across independent connections (impossible on H1's savepoint).

### H3 — `tests/database/test_learner_janitor_full_drain.py` (Testcontainers `neo4j:5.25`; LLM stubbed)

22. **`test_full_drain_reads_frozen_graph_from_real_neo4j`** (MANDATORY: a real full-drain test spinning a real Neo4j container) — using the Testcontainers `neo4j_client`: write a per-attempt `:_KGNode` subgraph and `stamp_graded_at(attempt_id=..., ts=<tz-aware>)` so a real `graded_at` exists; seed the matching course/session/attempt (pending, good report) on `db_session`; stub ONLY the LLM (`main_chat_adjudicator`/`main_chat_auditor`), NOT Neo4j. LAYER3 ON. Call `drain_pending_attempts(neo4j_client, limit=1)`. Assert: `build_rerun_inputs` read the real `graded_at` (the persisted `LearnerState.last_evidence_at` equals the stamped `ts`, NOT `now()` — proving the Δt anchor is the frozen instant), `learner_update_pending` cleared, `DrainResult(succeeded=1)`. This is the only test that exercises the real Neo4j read path end-to-end through the drain. Use NEGATIVE `attempt_id` only if required by the live-Aura cleanup convention — but the Testcontainers graph is wiped per-test, so a positive id is fine here.

**Coverage note:** tests 3-22 drive every branch of `drain_pending_attempts` + the helpers. There is NO `# pragma: no cover` in this unit (the un-coverable `while`/`asyncio.run` shell is WU-5B3b, not here). If `diff-cover` flags an uncovered branch, add a test — do NOT pragma it.

## 8. Owner-doc updates (drift contract)

Reconcile `docs/architecture/apollo.md` in the SAME commit (it already `owns: apollo/**`; `last_verified` is already `2026-06-19` — keep it). Concrete edits:

1. **Module map — `apollo/handlers/` row (line 33):** append a **WU-5B3a-1** clause registering `learner_janitor.py`:
   > **WU-5B3a-1** adds `learner_janitor.py` (`drain_pending_attempts(neo, *, limit=1, max_attempts=MAX_ATTEMPTS, user_id=None) -> DrainResult`) — the retry-janitor DRAIN state machine that CONSUMES the migration-028 columns. It claims one due `learner_update_pending` (not-dead) attempt with `FOR UPDATE SKIP LOCKED` + a `CLAIM_LEASE_S` work-lease (one short claim-txn that bumps `learner_update_attempts` and COMMITs immediately, mirroring `teacher_weekly._claim_upload_job_async`), re-runs the frozen `build_rerun_inputs` → `run_graph_simulation` → (LAYER3-gated) `run_learner_update` in a SEPARATE work session, then in a THIRD record-txn (re-SELECT by id, mirroring `_handle_job_failure_async`) clears `learner_update_pending` on success / sets `learner_update_failed_permanently` on `LearnerUpdateUnreconstructableError` (terminal, no backoff/LLM) / backs off `learner_update_next_attempt_at` by the pinned schedule (300/1200/3600 ±10% jitter) on transient failure, dead-lettering at `attempts >= MAX_ATTEMPTS=3`. The belief write is interlocked on `_graph_sim_layer3_enabled()` (the back-door guard): LAYER3 OFF → `run_graph_simulation` re-runs (supersede-idempotent) but the belief write is DEFERRED (row left pending, deferral does not consume a retry). The clear-flag lives in the janitor record-txn (NOT in frozen `run_learner_update`) to preserve the WU-5A2 byte-identity guardrail; a crash between the fold-commit and the clear can re-increment `evidence_count` by at most +1/entity (belief stays correct — accepted bound). Named constants `MAX_ATTEMPTS`/`BASE_DELAY_S`/`BACKOFF_FACTOR`/`MAX_DELAY_S`/`JITTER`/`CLAIM_LEASE_S` (env-overridable). NO worker loop / Procfile here (WU-5B3b).
2. **Done-time data-flow / §6.4 retry narrative:** wherever the doc describes `learner_update_pending` and "a future janitor" (e.g. the lifecycle row near line 66 mentioning "a future janitor prunes old subgraphs" is a DIFFERENT janitor — leave it; the learner-update janitor is new), add one sentence under the shadow/Layer-3 description: the offline drain that CLEARS `learner_update_pending` (set by `done_grading`/`learner_update`'s NO-FALLBACK forks) is `learner_janitor.drain_pending_attempts`; the two clocks are distinct — the claim/backoff schedule uses wall-clock `now()` (SQL), the belief Δt anchor uses the FROZEN Neo4j `graded_at` `done_ts` (never `now()`).
3. **`apollo/conftest.py` / test-harness note (optional, if a harness section exists):** note the three-harness composition for the janitor (H1 savepoint `db_session`, H2 two-connection asyncpg for SKIP-LOCKED, H3 Testcontainers `neo4j:5.25` for the real-Neo4j full drain) and that the H3 test lives under `tests/database/` so the Testcontainers `neo4j_client` is in scope (not the live-Aura `apollo/conftest.py` one).

Do NOT touch `docs/architecture/_overview.md` (the Procfile/worker-process ops surface is WU-5B3b, not this unit). Keep `last_verified: 2026-06-19`.

## 9. Verification commands (ALL local — executor runs these)

Use `.venv/Scripts/python.exe` (py3.12 — bare `python` lacks deps; an `ImportError` means wrong interpreter, NOT a blocker). Docker must be UP (Testcontainers pgvector:pg16 + neo4j:5.25). Run from `ai-ta-backend/`.

- [ ] H1 drain/accounting suite (real PG, Neo4j+LLM stubbed):
      `.venv/Scripts/python.exe -m pytest apollo/handlers/tests/test_learner_janitor.py -v`
      — expect: all GREEN, none skipped (Docker up).
- [ ] H2 contention + H3 full-drain (REAL-INFRA GATE — changed `tests/database/*`):
      `.venv/Scripts/python.exe -m pytest tests/database/test_learner_janitor_contention.py tests/database/test_learner_janitor_full_drain.py -v`
      — expect: GREEN, NOT skipped (a skip is a FAIL of this gate); the full-drain test spins a real `neo4j:5.25` container.
- [ ] Full `tests/database` real-infra gate (changed tests trigger it):
      `.venv/Scripts/python.exe -m pytest tests/database -v`
      — expect: GREEN-not-skipped.
- [ ] No apollo regressions (WU-5A2/5B1/5B2/5B3a-0 suites stay green):
      `.venv/Scripts/python.exe -m pytest apollo -q`
      — expect: all pass; in particular `test_done_shadow_route_postgres.py` (me==0/ls==0 byte-identity) and `test_build_rerun_inputs.py` are untouched-green.
- [ ] Patch coverage gate (>= 95% vs the foundation branch):
      `.venv/Scripts/python.exe -m pytest --cov=apollo --cov-report=xml -q`
      then `.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml --compare-branch=feat/apollo-kg-wu5b3a0-janitor-foundation --fail-under=95`
      (or `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b3a0-janitor-foundation --fail-under=95` if on PATH)
      — expect: `>= 95%` on the changed lines of `apollo/handlers/learner_janitor.py`. NO `# pragma: no cover` in this unit; cover every branch with a real test.
- [ ] Lint/format (project idiom): `.venv/Scripts/python.exe -m ruff check apollo/handlers/learner_janitor.py` (+ black/isort if wired) — expect: no new warnings.

**STOP — the executor's job ends here.** No migration is applied to any remote DB (028 is already on disk and local-Docker-only). No worker process is started. No PR is opened.

### Deploy handoff (HUMAN/CI only — never executed by feller)
Migration 028 was authored by WU-5B3a-0 and is applied LOCALLY only. At deploy time a human/CI rehearses it on the TEST Supabase project, then promotes to prod via the normal `staging → ApolloV3` flow. The janitor code ships dormant — there is no worker process or flag flip in this unit (that is WU-5B3b). Nothing in this plan is a remote-mutating command.

## 10. Risks (confidence-rated)

- **[HIGH] H2 SKIP-LOCKED proof is the load-bearing concurrency test and the hardest to write.** Two `drain_pending_attempts` coroutines each open their OWN `get_async_session()` (a per-event-loop engine, `session.py:98-120`). The proof must (a) ensure both run on the SAME asyncio loop (use `asyncio.gather`, not threads — the per-loop engine registry depends on it), (b) seed+COMMIT the due row before gathering, and (c) patch the WORK callees so the assertion isolates the claim. Mitigation: model the contention test on `test_apollo_learner_model_migration.py`'s committed-DB pattern; if `asyncio.gather` of two claims is racy-but-flaky, fall back to the explicit `test_claim_lease_prevents_reclaim_during_work` (drainer 1 paused mid-work via an `asyncio.Event`) which is deterministic and proves the SAME single-flight guarantee. Both are in the plan; at least one MUST pass green.
- **[HIGH] WORK-session connection held across the LLM calls (textbook class).** The frozen `run_graph_simulation` holds a connection across both LLM calls and commits internally at `done_grading.py:256`. `pool_recycle=1800` does NOT save a held-checked-out connection (it is already set, `session.py:94` — necessary-but-insufficient). Mitigation (all already true): the WORK session is FRESH per attempt (minutes-scale hold, refreshed by the internal commit), `pool_pre_ping=True` validates at the record-txn's next checkout, and a mid-LLM drop → outer `except` → clean record-txn backoff (test #14). Do NOT claim "no held connection" in the doc.
- **[MEDIUM] `evidence_count` +1 crash-window drift.** Clearing `pending` in the record-txn (separate from the fold-commit) means a crash between them re-folds idempotently in belief but increments `evidence_count` by +1/entity on re-drain. ACCEPTED + DOCUMENTED + TESTED (#12); do NOT attempt exactly-once (would require folding the clear into frozen `run_learner_update`, violating the byte-identity guardrail).
- **[MEDIUM] LAYER3-OFF defer must not spin or consume retries.** If the deferral did NOT undo the at-claim increment, a long LAYER3-OFF window would dead-letter healthy rows after 3 sweeps. The record-txn undoes the bump and re-arms `next_attempt_at=None` (test #11). The worker-loop pacing (WU-5B3b) prevents a hot re-drain spin; in THIS unit the test asserts the single-sweep accounting only.
- **[MEDIUM] vanished-problem `NoResultFound` classification.** `_find_problem_payload`'s `scalar_one()` raises a generic `NoResultFound` on a deleted problem (5B3a-0 heads-up #7b). If `build_rerun_inputs` does NOT already convert it to `LearnerUpdateUnreconstructableError`, the janitor's `except` MUST classify `sqlalchemy.exc.NoResultFound` as TERMINAL (dead-letter), not transient — otherwise a deleted problem crash-loops until MAX_ATTEMPTS (3 wasted LLM-free claims, then dead-letter anyway, but with misleading transient `last_error`). Test #8 pins the terminal behavior; the executor verifies which layer catches it during RED.
- **[LOW] two clocks, both correct.** Claim/backoff uses SQL `now()` (scheduling); the belief Δt anchor uses the frozen Neo4j `graded_at` (`fromisoformat`, tz-aware). A reviewer must not "unify" them. Documented in §8.2 and asserted by test #22 (`last_evidence_at == stamped ts`, not `now()`).
- **[LOW] jitter determinism in tests.** `random.uniform` must be injectable (`_backoff_delay_s(..., jitter_fn=...)`) so the schedule asserts are exact. Tests #1/#9 pin it; #2 asserts the band with a seeded `random`.
- **[LOW] comparison-run re-persist on LAYER3-OFF re-defer.** `persist_comparison_run` is supersede-idempotent (DELETE-first on `(attempt_id, comparison_version)`, `grading/persistence.py:231`), so the re-defer loop does not leak rows. Not separately tested here (covered by the WU-4C suite); noted so a reviewer does not flag it.

## 11. Out-of-scope boundaries (this unit)

DO NOT (these belong to WU-5B3b or are frozen):
- The worker loop / `apollo/learner_janitor_worker.py` / the `while not stop_event` shell — WU-5B3b.
- The `Procfile` `apollo-janitor` line and any Railway ops posture — WU-5B3b.
- The `done.py` opportunistic backstop hook — WU-5B3b (and DEFERRED per adjudication #2).
- The `APOLLO_LEARNER_JANITOR_ENABLED` flag read / SIGTERM handler — WU-5B3b. (This unit's `drain_pending_attempts` is callable directly; the flag gates the LOOP, not the drain.)
- Editing ANY frozen file: `run_graph_simulation` (`done_grading.py`), `run_learner_update` / `persistence.py`, `belief`/`update`/`state_model`/`decay`/`negotiation` (`learner_model/**`), `build_rerun_inputs` (`done_inputs.py` — REUSE it), `KGStore` (`store.py` — REUSE `read_node_graded_at`/`read_graph`), `models.py`, `errors.py` (the error class is already there), migration 028. The janitor ORCHESTRATES; it does not reimplement.
- Adding any new package (queue libs, `tenacity`, etc.) — `.with_for_update(skip_locked=True)` (core SQLAlchemy) + `asyncio`/`time`/`random`/`datetime` (stdlib) cover everything. Confirm-the-negative per adjudication #4.
- Applying migration 028 (or any DDL) to any remote Supabase project — local Testcontainers only.

## 12. Deviations the executor MAY take

- **Exact module-private helper names** (`_utc_now`, `_truncate`, `_backoff_delay_s`, `_int_env`) — keep the signatures the tests pin (`_backoff_delay_s(attempts, *, jitter_fn=...)` MUST be injectable), but bikeshed-rename freely otherwise.
- **`DrainResult` field set** — the six fields named in §2 are the contract the tests assert; if a field proves redundant (e.g. `retried` always derivable), the executor may drop it AND update the tests in the same RED pass — but keep `claimed/succeeded/dead_lettered/deferred/backlog_remaining` (the load-bearing observability + assertion targets).
- **Where the `NoResultFound`-→terminal classification lives** — either confirm `build_rerun_inputs` already converts it (then test #8 just asserts the dead-letter outcome), or add the `except NoResultFound` classification in the janitor's WORK-phase except (then test #8 asserts the janitor catches it). The plan requires the terminal OUTCOME; the executor picks the catch site after the RED run shows which layer raises.
- **H2 contention shape** — `asyncio.gather(drain, drain)` (test #19) vs the deterministic paused-mid-work lease test (#20). Ship BOTH if both are green; if #19 is flaky, #20 is the authoritative single-flight proof and #19 may be downgraded to a smoke assertion — but the SKIP-LOCKED guarantee MUST have one deterministic green proof.
- **H1 stub seam depth** — the executor may patch at the `run_graph_simulation`/`run_learner_update` boundary (coarser, faster) for the accounting tests where the assertion target is claim/record state, and only run the FULL chain (Neo4j-stubbed, LLM-stubbed) in the success/idempotency/LAYER3 tests where the belief side-effect is the target. Keep at least the success (#3), LAYER3-off (#11), and idempotency (#12) tests driving the real chain so the orchestration is genuinely exercised.

---

**provides:** `apollo/handlers/learner_janitor.py::drain_pending_attempts`, `DrainResult`, the six named constants.
**consumes:** `build_rerun_inputs`, `run_graph_simulation`, `run_learner_update`, `_graph_sim_layer3_enabled`, `KGStore.read_node_graded_at`/`read_graph`, `ProblemAttempt`/`ApolloSession` ORM, migration-028 columns, `get_async_session`.
**depends_on:** WU-5B3a-0 (foundation — already merged: migration 028, ORM cols, error class, `read_node_graded_at`, `build_rerun_inputs`).
