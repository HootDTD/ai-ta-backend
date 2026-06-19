# WU-5A2 implementation plan — all-or-nothing Layer-3 persist + Done wiring

**Date:** 2026-06-18
**Unit:** WU-5A2 (stacks on the FROZEN WU-5A1 pure core; branch `feat/apollo-kg-wu5a2-layer3-persist`)
**Authoritative contract:** `docs/superpowers/plans/2026-06-18-apollo-kg-wu5a-split-proposal.md` (§3 WU-5A2 + §4 LOCKED decisions + §6 key_risks + the ORCHESTRATOR ADJUDICATION block)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §3 (belief write path), §6.4 steps 16-17 (event emission + Layer-3 update in the Done txn), §6.6 (abstention is upstream/frozen), §8 (all-or-nothing Done transaction)
**Owner doc:** `docs/architecture/apollo.md` (reconcile + bump `last_verified` to 2026-06-18 in the same commit)
**Compare branch (diff-cover):** `feat/apollo-kg-wu5a1-bayesian-filter-core` — patch coverage of changed lines MUST be >= 95% (target 100%)

---
provides:
  - apollo/learner_model/persistence.py (persist_learner_update, LearnerUpdateResult, *_orm_from_spec)
  - apollo/handlers/learner_update.py (run_learner_update — the single commit + NO-FALLBACK)
  - apollo/handlers/done.py wiring (_GRAPH_SIM_LAYER3_FLAG, done_ts single-capture, gated call, parser_confidence)
  - apollo/knowledge_graph/store.py stamp_graded_at(*, attempt_id, ts=None) back-compat widening
  - tests/database/test_done_layer3_route_postgres.py (real-PG Testcontainers acceptance gate)
consumes:
  - apollo/learner_model/{belief,update,state_model}.py (FROZEN WU-5A1 API — apply_event / event_to_row_specs / COLD_START_PRIOR)
  - apollo/grading/events.py (convert_findings_to_events — step 16, frozen WU-4B2)
  - apollo/grading/abstention.py (min_parser_confidence_of — frozen §6.6 helper)
  - apollo/handlers/done_grading.py (ShadowGradeResult, run_graph_simulation, _set_pending_and_commit pattern, learner_update_pending)
  - apollo/knowledge_graph/canon_projection.py (load_entity_specs — canonical_key -> entity_id)
  - apollo/persistence/models.py (MasteryEvent / LearnerState / ProblemAttempt ORM; migration 026 schema)
depends_on:
  - WU-5A1 (feat/apollo-kg-wu5a1-bayesian-filter-core)
  - WU-4C1 (ShadowGradeResult + done_grading wiring), WU-4B2 (events), WU-3C1 (load_entity_specs)
---

## Overview

WU-5A2 wires the FROZEN WU-5A1 pure belief core into the LIVE Done transaction and
persists Layer-3 (`apollo_mastery_events` append + `apollo_learner_state` upsert) in ONE
all-or-nothing Postgres transaction, behind a NEW flag `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
(default OFF everywhere). It is §6.4 step 16 INVOCATION + step 17 (the §3 belief write).

What changes:
1. A NEW `persist_learner_update(...)` (FLUSH-ONLY, no commit) calls `convert_findings_to_events`
   (step 16), resolves each event's `canonical_key -> entity_id`, `SELECT ... FOR UPDATE`s the
   prior learner-state rows, recomputes the base from the EVENT LOG (not the self-mutated state
   row), applies WU-5A1's `apply_event`, supersedes this attempt's prior events, and appends +
   upserts the two tables.
2. A NEW `run_learner_update(...)` OWNS the single `commit()` (the atomic boundary) and the
   NO-FALLBACK fork: on failure it rolls back, sets `attempt.learner_update_pending=True`,
   commits THAT flag in a separate txn, and re-raises a named error that NEVER voids the
   already-committed grade (mirrors the WU-4C1 `_set_pending_and_commit` pattern).
3. `done.py` captures ONE `done_ts = datetime.now(UTC)`, threads it into BOTH
   `stamp_graded_at(ts=done_ts)` AND `run_learner_update(done_ts=done_ts)`, adds the new flag,
   and makes the gated call AFTER the shadow persist with
   `parser_confidence = min_parser_confidence_of(student_graph.nodes)` and
   `grader_confidence = shadow.normalization_confidence`.
4. `store.py` widens `stamp_graded_at` to accept an optional `ts` so Neo4j `n.graded_at` and
   Postgres `last_evidence_at` stamp the IDENTICAL instant.

Flag OFF (the default, and prod) -> the gated call never fires -> `done.py`'s return is
byte-identical to today -> the existing `test_done_shadow_route_postgres.py` empty-table
assertions (`me==0`, `ls==0`) stay GREEN (the flag-off regression guard).

**No migration.** Migration 026 already shipped both tables + every CHECK + the UNIQUE NULLS
NOT DISTINCT + `learner_update_pending`. Next free number is 028; WU-5A2 uses none.

## Structural prep (neighborhood scan)

Scanned every artifact in the change path (the two new modules, `done.py`'s `handle_done`,
`store.py`'s `stamp_graded_at`) and their one-ring dependents.

- **`done.py` `handle_done` function size** — currently ~165 lines (`done.py:237-402`), already
  the largest function in scope. WU-5A2 ADDS only ~12 lines (one `done_ts` capture, one flag
  helper, one gated `if` block computing `parser_confidence` + the awaited call). It stays
  under the 800-line file ceiling and is NOT refactored here: the shadow block it sits next to
  is the established WU-4C1/4C2 pattern, and splitting `handle_done` is out of WU-5A2's seam
  (it would touch the frozen shadow wiring). Net: no structural prep, the addition is minimal
  and mirrors the existing `_graph_sim_shadow_enabled()` / `_graph_sim_live_enabled()` shape.
- **`apollo_mastery_events` / `apollo_learner_state` fan-in** — these two tables have ZERO
  current writers (WU-4C1's `test_no_mastery_events_written` tripwire proves it) and the only
  readers are the `tests/database` guards. WU-5A2 is the FIRST and ONLY writer. No coupling hub.
- **`stamp_graded_at` fan-in** — grep shows two callers: `done.py:345` and the shadow-route
  test stub (`test_done_shadow_route_postgres.py:99`). The widening is back-compat (new optional
  `ts=None`), so neither caller breaks; the test stub is an `AsyncMock` that ignores kwargs.
- **Access-policy sprawl** — N/A: these are course-scoped Postgres tables behind the owner
  connection; RLS is default-deny-no-policy (migration 026 §7, the app-layer `search_space_id`
  predicate is the enforcement point). WU-5A2 adds NO policy and NO new table.

**Conclusion: neighborhood is clean.** No structural prep precedes the change. (Budget: 0 of
the plan's steps are debt-reduction — well under the 30% cap.)

## Prior art (existing modules)

Mirror these shipped patterns verbatim — do NOT invent new shapes.

1. **`apollo/grading/persistence.py` (WU-4B3)** — the canonical "pure `*_to_row` spec +
   thin async write seam, flush-only, supersede-by-delete" persistence module.
   - `persist_comparison_run` (`grading/persistence.py:197-246`): builds specs, `DELETE`s the
     prior run in the SAME txn (supersede), `db.add` + `db.flush()` to materialize the id,
     bulk-adds children, **does NOT `commit()`** ("the caller owns the transaction boundary",
     docstring lines 24/216). `persist_learner_update` copies this exactly.
   - `_run_orm_from_spec` / `_finding_orm_from_spec` (`:156-194`): spec -> ORM helpers, tuples
     list-ified for the JSONB column. WU-5A2's `_mastery_event_orm_from_spec` /
     `_learner_state_orm_from_spec` mirror them.
2. **`apollo/handlers/done_grading.py` (WU-4C1)** — the NO-FALLBACK transaction-owner pattern.
   - `_set_pending_and_commit` (`done_grading.py:156-162`): `attempt.learner_update_pending = True;
     await db.commit()`. WU-5A2's `run_learner_update` reuses this exact two-line shape for the
     failure fork.
   - `run_graph_simulation` (`:165-322`): owns the run-txn `db.commit()` AFTER the flush-only
     `persist_comparison_run`, wraps the cross-store window in `try/except` that catches named
     infra errors -> set pending + re-raise, and a bare `except Exception:` catch-all that ALSO
     sets pending + re-raises (`:313-322`). `run_learner_update` is the structural twin (simpler:
     no Neo4j, no LLM).
   - `load_entity_specs` + the map build (`done_grading.py:187-190`):
     `{spec.canonical_key: spec.key for spec in specs}` is the EXACT canonical_key -> entity_id
     map WU-5A2 re-derives.
3. **`apollo/knowledge_graph/canon_projection.py` (WU-3C1)** — `load_entity_specs(db, *,
   search_space_id=, concept_id=) -> list[CanonNodeSpec]` (`:117-173`); `spec.key` is the
   surrogate `apollo_kg_entities.id` (int), `spec.canonical_key` is the string. Course-scoped,
   raises `CanonProjectionError` on DB failure.
4. **`apollo/learner_model/update.py` + `state_model.py` (FROZEN WU-5A1)** — `apply_event(...)`
   `-> BeliefUpdate` and `event_to_row_specs(event, update, *, user_id, search_space_id,
   entity_id, attempt_id) -> (MasteryEventRowSpec, LearnerStateRowSpec)`. The `LearnerStateRowSpec`
   has NO `last_evidence_at` (WU-5A2 sets it to `done_ts`) and `evidence_count` defaults to 1
   (WU-5A2 must INCREMENT it on conflict, not blindly write the literal 1).
5. **`done.py` flag convention (WU-4C1/4C2)** — `_GRAPH_SIM_SHADOW_FLAG` / `_GRAPH_SIM_LIVE_FLAG`
   constants + `_graph_sim_shadow_enabled()` / `_graph_sim_live_enabled()` helpers
   (`done.py:63-82`): `os.environ.get(FLAG, "").lower() in ("1", "true", "yes")`. The new
   `_GRAPH_SIM_LAYER3_FLAG` + `_graph_sim_layer3_enabled()` copy this exactly.
6. **`tests/database/test_done_shadow_route_postgres.py` (WU-4C1)** — the real-PG harness shape
   the new test copies: `pytestmark = pytest.mark.integration`, `db_session` fixture, `seed_course`
   from `_curriculum_fixtures`, the `_neo_stubs` patch list (read_graph / freeze / stamp_graded_at
   / write_resolution / read_node_created_at / adjudicator / auditor), `monkeypatch.setenv(FLAG)`.

## Scope: files to create / edit

**CREATE (3):**
| File | Role |
|---|---|
| `apollo/learner_model/persistence.py` | `persist_learner_update` (flush-only) + `LearnerUpdateResult` + spec->ORM helpers. The real-PG persistence seam. ~180-220 lines. |
| `apollo/handlers/learner_update.py` | `run_learner_update` — re-derives the entity map, calls `persist_learner_update`, owns the single `commit()`, NO-FALLBACK fork. ~70-90 lines. |
| `tests/database/test_done_layer3_route_postgres.py` | Real-PG Testcontainers acceptance gate (`pytest.mark.integration`, `db_session`). All constraint + atomicity + retry + done_ts tests. |

**EDIT (3):**
| File | Change (line budget) |
|---|---|
| `apollo/handlers/done.py` | ~12 lines: `from datetime import UTC, datetime`; `from apollo.grading.abstention import min_parser_confidence_of`; `from apollo.handlers.learner_update import run_learner_update`; `_GRAPH_SIM_LAYER3_FLAG` const + `_graph_sim_layer3_enabled()` helper; capture `done_ts = datetime.now(UTC)` once at the stamp site; pass `ts=done_ts` to `stamp_graded_at`; add the gated `run_learner_update` call after the shadow block. |
| `apollo/knowledge_graph/store.py` | ~3 lines: widen `stamp_graded_at(self, *, attempt_id)` to `stamp_graded_at(self, *, attempt_id, ts: str | datetime | None = None)`; normalize `ts` to ISO (`ts.isoformat()` if datetime, else the string), fall back to `_utc_now_iso()` when `ts is None`. |
| `docs/architecture/apollo.md` | Owner-doc drift reconciliation (see the dedicated section) + bump `last_verified` to 2026-06-18. |

**READ-ONLY (do NOT modify — frozen):** `apollo/learner_model/{belief,update,state_model}.py`,
`apollo/grading/events.py`, `apollo/grading/abstention.py`, `apollo/handlers/done_grading.py`,
`apollo/canon_projection.py` (`load_entity_specs`), `apollo/persistence/models.py`,
`database/migrations/026_apollo_learner_model.sql`,
`tests/database/test_done_shadow_route_postgres.py` (the flag-OFF byte-identity guard — keep
green, do not edit its assertions).

## Public signatures (frozen)

```python
# apollo/learner_model/persistence.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping
from sqlalchemy.ext.asyncio import AsyncSession
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.persistence.models import ApolloSession, ProblemAttempt

@dataclass(frozen=True)
class LearnerUpdateResult:
    events_written: int                 # rows appended to apollo_mastery_events
    states_upserted: int                # distinct (user, search_space, entity) upserts
    skipped_unmapped: tuple[str, ...]   # canonical_keys with no entity_id (event skipped)
    abstained: bool                     # True when convert_findings_to_events() == ()

async def persist_learner_update(
    db: AsyncSession,
    *,
    sess: ApolloSession,
    attempt: ProblemAttempt,
    shadow: ShadowGradeResult,
    done_ts: datetime,
    parser_confidence: float,
    canon_key_by_canonical_key: Mapping[str, int],
) -> LearnerUpdateResult: ...
    # FLUSH-ONLY. Does NOT commit (the caller owns the txn boundary). Steps:
    # convert_findings_to_events -> resolve entity_id -> attempt-wide supersede DELETE ->
    # SELECT ... FOR UPDATE priors -> recompute base from EVENT LOG -> apply_event ->
    # append MasteryEvent rows + upsert LearnerState (evidence_count INCREMENT, last_evidence_at=done_ts).

# apollo/handlers/learner_update.py
async def run_learner_update(
    db: AsyncSession,
    *,
    sess: ApolloSession,
    attempt: ProblemAttempt,
    shadow: ShadowGradeResult,
    done_ts: datetime,
    parser_confidence: float,
) -> LearnerUpdateResult | None: ...
    # Re-derives canon_key_by_canonical_key via load_entity_specs(db,
    # search_space_id=sess.search_space_id, concept_id=sess.concept_id), calls
    # persist_learner_update, then db.commit() (the all-or-nothing boundary). On ANY
    # failure inside the txn: rollback, set attempt.learner_update_pending=True, commit
    # THAT flag in a separate txn, re-raise the original error (NO-FALLBACK). Returns None
    # only if it is never reached (the done.py gate guards shadow is not None).

# apollo/handlers/done.py
_GRAPH_SIM_LAYER3_FLAG: str = "APOLLO_GRAPH_SIM_LAYER3_ENABLED"   # default OFF everywhere
def _graph_sim_layer3_enabled() -> bool: ...

# apollo/knowledge_graph/store.py  (BACK-COMPAT widening — the no-ts call stays valid)
async def stamp_graded_at(self, *, attempt_id: int, ts: str | datetime | None = None) -> int: ...
```

The `done.py` gated call site (after the existing shadow block, inside `if _graph_sim_shadow_enabled():`):
```python
if shadow is not None and _graph_sim_layer3_enabled():
    parser_confidence = min_parser_confidence_of(student_graph.nodes)
    await run_learner_update(
        db, sess=sess, attempt=attempt, shadow=shadow,
        done_ts=done_ts, parser_confidence=parser_confidence,
    )
```
`grader_confidence` is NOT computed in `done.py`; `persist_learner_update` derives it as
`shadow.normalization_confidence * 1.0` (comparison_confidence = 1.0 in v1, §3 line 432-435).

## Behavioral contract (the load-bearing rules)

These are the acceptance-blocking invariants. Each maps to a pinned test below.

1. **Flag OFF byte-identity.** With `APOLLO_GRAPH_SIM_LAYER3_ENABLED` unset/false, the gated
   call NEVER fires. `done.py`'s return dict and the DB side-effects are byte-identical to
   today. `test_done_shadow_route_postgres.py:151-160` (`me==0`, `ls==0`) stays GREEN UNCHANGED.

2. **One commit, all-or-nothing (§8 / §3 line 456-461).** `persist_learner_update` FLUSHES
   ONLY. `run_learner_update` issues the SINGLE `commit()`. The mastery-event appends AND the
   learner-state upserts live in ONE transaction: a crash between them rolls back BOTH tables.
   A partial write is cascading read-modify-write corruption — forbidden.

3. **NO-FALLBACK never voids the grade.** The OLD grade/XP and the shadow run/findings are
   already committed before `run_learner_update` runs. On ANY exception inside the Layer-3 txn:
   rollback the Layer-3 work, set `attempt.learner_update_pending=True`, commit THAT flag in a
   separate tiny txn, re-raise. The student grade (`attempt.result == "graded"`) stays committed.

4. **convert_findings_to_events is called HERE (step 16), NEVER in done_grading.py.** The first
   and only caller is `persist_learner_update`. `done_grading.py`'s `test_no_mastery_events_written`
   tripwire stays GREEN. Called as
   `convert_findings_to_events(shadow.audited, opposes_map=shadow.opposes_map, turn_order=shadow.turn_order)`.
   `() ` (abstained / empty / fully-suppressed) -> write NOTHING, return `LearnerUpdateResult(0, 0, (), abstained=True)`.

5. **q sourcing (§3 line 430, finding #8).** `q = parser_confidence * grader_confidence` ONLY.
   **[AMENDED post-build — see "Post-build amendment" at the end.]** q is formed inside the
   per-entity **combined-likelihood fold** `_combined_belief_update` (multiply every event's
   `likelihood_for_event` into one `L`, `damp` once, one `bayes_update`), NOT a per-event
   `apply_event` chain — the §3.1 affine damper is not multiplicative-homomorphic, so chaining
   would diverge for `q < 1`. For a single event the two are identical. `parser_confidence =
   min_parser_confidence_of(student_graph.nodes)` (computed in `done.py`); `grader_confidence =
   shadow.normalization_confidence * 1.0` (computed in `persist_learner_update`). The COVERED
   `event.score` is already resolution-scaled; `event.confidence` is NOT re-multiplied into q.

6. **canonical_key -> entity_id JOIN, unmapped => SKIP.** `LearnerEvent.canonical_key` is the
   resolved reference-node key (e.g. `eq.continuity`), which equals `apollo_kg_entities.canonical_key`.
   Resolve via the injected `canon_key_by_canonical_key` map (`{spec.canonical_key: spec.key}`).
   An event whose key is absent from the map is SKIPPED (recorded in `skipped_unmapped`), NEVER
   inserted with a NULL/stub `entity_id` (both `entity_id` columns are NOT NULL FKs). Empty
   pre-promotion specs (no entities seeded) => ALL events skipped, tolerated (no crash).

7. **entity_id REQUIRED before write.** Pass the resolved int `entity_id` into
   `event_to_row_specs(..., entity_id=<int>, user_id=sess.user_id,
   search_space_id=sess.search_space_id, attempt_id=attempt.id)`. Never call the persist with
   `entity_id=None` (assert/guard before the ORM build).

8. **SUPERSEDE / RETRY recompute base = the EVENT LOG, never the self-mutated state row
   (finding #6).** On a retry of the SAME attempt:
   - FIRST, attempt-wide DELETE: `DELETE FROM apollo_mastery_events WHERE attempt_id = :attempt_id`
     (ALL entity/kind — covers a misconception->corrected kind change across runs). Same txn.
   - For each affected `(user, entity)`, the recompute base is the `posterior_belief` of the
     `apollo_mastery_events` row with `MAX(created_at) WHERE entity_id = :e AND attempt_id IS
     DISTINCT FROM :attempt_id`; else `COLD_START_PRIOR` (pass `prior_belief=None` to
     `apply_event`). NEVER read the base from `LearnerState.belief` (it may already carry this
     attempt's first-run mutation). `prior_last_evidence_at` = the prior event-log row's
     anchor (the corresponding `LearnerState.last_evidence_at` BEFORE this update, read under the
     FOR UPDATE lock; `None` when no prior).

9. **SELECT ... FOR UPDATE.** Lock the affected `LearnerState` rows before the read-modify-write:
   `select(LearnerState).where(...).with_for_update()`. A janitor retry racing a live Done must
   not clobber a posterior.

10. **evidence_count INCREMENTS on upsert; 1 ONLY on fresh insert.** On an existing
    `LearnerState` row, set `evidence_count = existing.evidence_count + 1`. On a fresh insert,
    set `evidence_count = 1`. Do NOT blindly write the `LearnerStateRowSpec` default literal 1
    onto an existing row.

11. **last_evidence_at = done_ts; updated_at = done_ts.** The persist stamps the upserted
    `LearnerState.last_evidence_at` and `updated_at` to the handler-captured `done_ts` (the
    SAME instant Neo4j `n.graded_at` carries via `stamp_graded_at(ts=done_ts)`). There is NO
    Postgres Done timestamp on `ProblemAttempt` — `done_ts` is the single source.

12. **done_ts single capture.** `done.py` captures ONE `done_ts = datetime.now(UTC)` and threads
    it into BOTH `stamp_graded_at(ts=done_ts)` and `run_learner_update(done_ts=done_ts)`. No
    second clock.

## TDD-ordered implementation

Strict RED→GREEN per step. Every test below is real-PG (`pytest.mark.integration`, `db_session`)
UNLESS marked "(flag-off, existing)". Use `.venv/Scripts/python.exe`.

1. **Back-compat widen `stamp_graded_at` (store.py).** RED: a unit test asserts
   `stamp_graded_at(attempt_id=…, ts=dt)` writes `dt.isoformat()` and the no-`ts` call still
   writes `_utc_now_iso()`. GREEN: add `ts: str | datetime | None = None`, normalize. Keep the
   two existing callers green (back-compat). This is the smallest, lowest-risk edit — do it first
   so the later done_ts-identity test has a target.
2. **`persist_learner_update` happy path (flush-only).** RED: `test_layer3_persists_events_and_state`
   — seed a course + entities (so `load_entity_specs` is non-empty), build a `ShadowGradeResult`
   with ≥1 covered finding, call `persist_learner_update` then `db.commit()` from the test, assert
   one `apollo_mastery_events` row per mapped event + one `apollo_learner_state` row with the
   posterior belief/mastery and `last_evidence_at == done_ts`. GREEN: implement convert→resolve→
   FOR UPDATE→recompute→apply_event→append+upsert, flush-only.
3. **entity_id unmapped → SKIP (contract 6).** RED: `test_unmapped_canonical_key_skipped` — an
   event whose `canonical_key` is absent from the entity map produces NO row and appears in
   `skipped_unmapped`; empty specs => all skipped, no crash, `abstained==False` (events existed,
   just unmapped). GREEN: the skip branch.
4. **abstention empty (contract 4).** RED: `test_abstained_writes_nothing` — `convert_findings_to_events()`
   returns `()` => zero rows both tables, `LearnerUpdateResult(0,0,(),abstained=True)`.
5. **`run_learner_update` single commit + entity-map re-derive.** RED:
   `test_run_learner_update_commits` — call the handler (not the persist), assert rows are
   committed (visible in a fresh session). GREEN: re-derive `canon_key_by_canonical_key` via
   `load_entity_specs`, call persist, `db.commit()`.
6. **All-or-nothing rollback (contract 2).** RED: `test_layer3_atomic_rollback` — monkeypatch the
   learner-state upsert to raise AFTER the mastery-event appends flush; assert BOTH tables are
   empty afterward (the whole txn rolled back). GREEN: ensure appends + upsert share ONE txn and
   the single commit is in `run_learner_update`.
7. **NO-FALLBACK never voids the grade (contract 3).** RED: `test_layer3_failure_sets_pending_keeps_grade`
   — force a Layer-3 exception; assert the named error propagates, `attempt.learner_update_pending
   is True` (committed), and `attempt.result == "graded"` survives. GREEN: the rollback→set-pending→
   commit-flag→re-raise fork (mirror `_set_pending_and_commit`).
8. **SUPERSEDE recompute from EVENT LOG (contract 8) — the two acceptance-blocking retry tests.**
   RED: `test_retry_identical_idempotent` — run twice with the same shadow; assert the final
   belief equals the single-run belief (attempt-wide DELETE + recompute from the prior-attempt
   event-log base, NOT the self-mutated state row). RED: `test_retry_changed_kind_recomputes` — first
   run emits a MISCONCEPTION event, retry emits a CORRECTED event for the same entity; assert the
   posterior reflects ONLY the corrected event over the prior-attempt base (no double-count, no
   misconception residue from the superseded run). GREEN: the `DELETE … WHERE attempt_id=` +
   `MAX(created_at) WHERE attempt_id IS DISTINCT FROM` recompute.
9. **evidence_count increments (contract 10).** RED: `test_evidence_count_increments` — a second
   distinct attempt for the same (user,entity) sets `evidence_count == 2`; a fresh insert is 1.
   GREEN: `existing.evidence_count + 1` on conflict.
10. **done_ts single instant (contracts 11,12).** RED: `test_done_ts_single_instant` — drive the
    full Done route with the flag ON; assert the persisted `LearnerState.last_evidence_at` equals
    the `ts` passed to the (stubbed) `stamp_graded_at`. GREEN: capture once in `done.py`, thread both.
11. **UNIQUE NULLS NOT DISTINCT + CHECKs (real-PG constraints).** RED:
    `test_belief_length_3_check` / `test_score_mastery_range_checks` / `test_unique_attempt_entity_kind`
    — attempt a length-2 belief, an out-of-range score, and a duplicate `(attempt_id,entity_id,event_kind)`;
    assert each raises an `IntegrityError` from the REAL CHECK/constraint (these MUST run on PG, not
    SQLite). GREEN: ensure the ORM build emits well-formed rows so these only trip on genuine violations.
12. **Flag-off byte-identity (contract 1) — existing guard.** Run
    `tests/database/test_done_shadow_route_postgres.py` unchanged; assert `me==0`, `ls==0` still
    green with the new code on the path but the flag OFF. Add `test_layer3_flag_off_no_write`
    mirroring it for the new route module.
13. **`done.py` wiring + flag helper.** RED: `test_layer3_flag_helper` (unit) — env unset/false/`"1"`/`"true"`.
    GREEN: `_GRAPH_SIM_LAYER3_FLAG` + `_graph_sim_layer3_enabled()` + the gated call block.
14. **Owner-doc reconciliation + `last_verified` bump** (same commit as the code).
15. **Full verify + diff-cover ≥95%.**

## Test plan (write tests FIRST)

`tests/database/test_done_layer3_route_postgres.py` (real-PG, `pytest.mark.integration`, `db_session`,
`seed_course` + entity seed, `_neo_stubs` patch list copied from `test_done_shadow_route_postgres.py`,
`monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED","1")` for the ON tests):

| Test | Contract |
|---|---|
| `test_layer3_persists_events_and_state` | 2,7,11 (happy path) |
| `test_unmapped_canonical_key_skipped` | 6 |
| `test_abstained_writes_nothing` | 4 |
| `test_run_learner_update_commits` | 2 (commit boundary) |
| `test_layer3_atomic_rollback` | 2 |
| `test_layer3_failure_sets_pending_keeps_grade` | 3 |
| `test_retry_identical_idempotent` | 8 |
| `test_retry_changed_kind_recomputes` | 8 |
| `test_evidence_count_increments` | 10 |
| `test_done_ts_single_instant` | 11,12 |
| `test_belief_length_3_check` | real CHECK |
| `test_score_mastery_range_checks` | real CHECK |
| `test_unique_attempt_entity_kind` | UNIQUE NULLS NOT DISTINCT |
| `test_layer3_flag_off_no_write` | 1 |

Unit tests (no DB): `test_graph_sim_layer3_enabled` (flag helper, in the existing done-handler unit
test module), `test_stamp_graded_at_accepts_ts` (store unit test). The existing
`test_done_shadow_route_postgres.py` and `done_grading.py`'s `test_no_mastery_events_written` MUST
stay green UNCHANGED (the boundary tripwires).

**Coverage:** every new line in `persistence.py` + `learner_update.py` + the `done.py`/`store.py`
edits must be hit; `diff-cover --compare-branch=feat/apollo-kg-wu5a1-bayesian-filter-core
--fail-under=95` (target 100%). The skip/abstain/rollback/pending branches are all reachable from
the tests above — do NOT leave a defensive branch untested; if one is unreachable, delete it.

## Owner-doc updates (drift contract)

In `docs/architecture/apollo.md`, SAME commit as the code:
- Add `apollo/handlers/learner_update.py` + `apollo/learner_model/persistence.py` to the module map
  with one-line roles (the Layer-3 all-or-nothing write seam).
- In the Apollo Done-flow / data-flow narrative, add step 17: "when `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
  is on, after the shadow persist the Done txn appends `apollo_mastery_events` + upserts
  `apollo_learner_state` (the §3 Bayesian belief) all-or-nothing; failure sets
  `learner_update_pending` without voiding the grade." Note the flag defaults OFF (shadow/gated).
- State that `stamp_graded_at` now accepts `ts` so Neo4j `graded_at` and Postgres `last_evidence_at`
  share one instant.
- Bump `last_verified: 2026-06-18`. Do NOT touch any other doc's `owns:` globs (learner_model is
  already owned by apollo.md from WU-5A1).

## Verification commands (ALL local — `.venv/Scripts/python.exe`)

```bash
# Gate 1 — apollo suite (no regressions)
python -m pytest apollo -q --tb=short
# Gate 2 — REAL-INFRA (triggers: changed files under tests/database/**). Docker UP (pg16).
python -m pytest tests/database -q          # MUST be green-not-skipped; constraint tests on PG
# Gate 3 — patch coverage
python -m pytest --cov=apollo --cov-report=xml -q
diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5a1-bayesian-filter-core --fail-under=95
ruff check apollo tests/database
```
Acceptance: Gate 1 zero failures; Gate 2 the new PG file + the existing shadow PG file green-not-skipped
(if Testcontainers SKIP, Docker is down — STOP, do not accept a skipped gate); Gate 3 diff-cover ≥95%.

## Deploy handoff (HUMAN/CI only)

- **No migration** (026 already shipped the tables/CHECKs/UNIQUE/`learner_update_pending`). Nothing
  to apply to any remote DB. Agents never touch remote Supabase.
- `APOLLO_GRAPH_SIM_LAYER3_ENABLED` ships **OFF** (prod + staging). Flipping it ON is a later HUMAN
  calibration decision (gated on WU-4C2 shadow metrics), NOT part of this build — same posture as
  `APOLLO_GRAPH_SIM_LIVE_ENABLED`.
- `learner_update_pending=true` rows are drained by the WU-5B retry janitor (not built here); until
  then they are inert (flag off => never set).

## Risks (confidence-rated)

- **HIGH — supersede double-count** if the recompute base is read from the self-mutated
  `LearnerState.belief` instead of the event log. Mitigated by contract 8 + the two retry tests
  (`test_retry_identical_idempotent`, `test_retry_changed_kind_recomputes`) which FAIL on a
  state-row base. Confidence base is correct: HIGH after tests green.
- **HIGH — partial write corruption** if appends and upsert aren't one txn. Mitigated by flush-only
  persist + single commit in the handler + `test_layer3_atomic_rollback`. Confidence: HIGH.
- **MED — `done_ts` drift** (two clocks) → Neo4j and PG disagree on the freeze instant. Mitigated by
  single capture + `test_done_ts_single_instant`. Confidence: HIGH.
- **MED — flag-off regression** breaking the live Done path. Mitigated by the unchanged
  `test_done_shadow_route_postgres.py` guard + `test_layer3_flag_off_no_write`. Confidence: HIGH.
- **LOW — entity map empty pre-promotion** → everything skipped. By design (contract 6), tested.
- **LOW — `ApolloSession`/`ProblemAttempt` field names** (`search_space_id`, `concept_id`, `user_id`,
  `result`, `learner_update_pending`) — executor MUST confirm against `models.py`, not assume.

## Out-of-scope boundaries (do NOT do here)

- **Decay** (`dt_days_since_last` is recorded by WU-5A1 but NOT applied) → WU-5B.
- **The `learner_update_pending` retry janitor** (drain/recompute) → WU-5B.
- **RQ5 hedge / negotiation-move multiplier** (`negotiation_move` stays NULL) → WU-5B.
- **Flipping `APOLLO_GRAPH_SIM_LAYER3_ENABLED` ON** → human calibration, later.
- **Wiring `opposes_map`** (still structurally empty from WU-4C1) — Layer-3 writes events with the
  empty map (conflict/corrected detection inert); the opposes wiring is its own carried follow-up,
  NOT WU-5A2. Do not expand `load_entity_specs`/`CanonNodeSpec` here.
- **Any migration**, any Neo4j schema change, any LLM call, any edit to the frozen WU-5A1 package or
  to `graph_compare`/`grading` internals.
- **Refactoring `handle_done`** (stays as-is; only the ~12-line addition).

## Deviations I'd allow the executor (small, log them)

- Exact ORM upsert mechanism (`insert(...).on_conflict_do_update` vs SELECT-FOR-UPDATE-then-branch) —
  either is fine as long as evidence_count increments correctly and the whole thing is one txn under
  the FOR UPDATE lock. Prefer the explicit SELECT-FOR-UPDATE branch (clearer for the recompute).
- `LearnerUpdateResult` field set may gain a field if a test needs it; it must stay a frozen
  dataclass.
- Helper private functions (`_mastery_event_orm_from_spec`, `_recompute_base_for_entity`) may be
  split/named as the executor sees fit, mirroring `grading/persistence.py`.
- If `load_entity_specs` needs `concept_id` and `sess.concept_id` can be None pre-cutover, fall back
  to `search_space_id`-only scoping (log it) — but the entity map being empty is already tolerated.

A MEDIUM+ deviation (changing a behavioral contract 1–12, touching a frozen unit, or skipping the
real-PG gate) is NOT allowed — ESCALATE to the orchestrator instead.

## Post-build amendment (2026-06-18) — combined-likelihood fold supersedes the per-event chain

**What shipped differs from this plan's per-event `apply_event` mechanism, and the difference is
the correct one (the plan carried a latent spec-divergence; the code is spec-faithful).**

The executor implemented the multi-event-per-entity belief update as a **single combined-likelihood
update** (`apollo/learner_model/persistence.py::_combined_belief_update`): for one entity in one
Done, start `L = [1,1,1]`, multiply in every event's frozen `likelihood_for_event(event)`, `damp`
the combined `L` ONCE with `q = parser·grader`, then ONE `bayes_update` over the event-log base.
This deliberately does NOT chain WU-5A1's `apply_event` per event.

**Why it is correct (spec §3 lines 393/420/433/454).** §3 says the belief is "updated once per
entity per [Done]", you "start `[1,1,1]`, multiply per evidence item", apply the confidence damper
`q` as a single Step 2, and the whole update "fires once per Done episode (never per turn)."
Chaining `apply_event` would apply the §3.1 affine damper `q·L + (1−q)·[1,1,1]` once PER event —
and because that damper is not multiplicatively homomorphic
(`[q·L₁+(1−q)]·[q·L₂+(1−q)] ≠ q·(L₁·L₂)+(1−q)` for `q<1`), the chained result diverges from the
spec whenever an entity has ≥2 events and `q<1`. For the common single-event-per-entity case the
two are byte-identical.

**No reimplementation of the math.** `_combined_belief_update` composes only the FROZEN WU-5A1
exports (`likelihood_for_event`, `damp`, `bayes_update`, `mastery_of`, `confidence_of`,
`misconception_code_of`). WU-5A1's `apply_event` remains the correct one-event primitive (used by
the single-event tests and available to WU-5B); the episode-level fold lives in WU-5A2. Each event
still appends its OWN `apollo_mastery_events` row (per-event detail preserved) but all rows of one
entity carry the SAME combined base→posterior transition, and the entity gets ONE
`apollo_learner_state` upsert. The misconception code surfaces against the COMBINED posterior
(last clearing event wins, deterministic converter order).

This amendment supersedes the "formed INSIDE `apply_event`" wording in contract 5 and the
`apply_event`-chain framing in the frozen-signature block / TDD step 8. Confirmed spec-faithful by
the orchestrator (independent §3 read + `_combined_belief_update` review) and by all three cold-eyes
lenses (correctness PASS, drift PASS-WITH-NITS, test-honesty PASS-WITH-NITS, 0 blocking).
