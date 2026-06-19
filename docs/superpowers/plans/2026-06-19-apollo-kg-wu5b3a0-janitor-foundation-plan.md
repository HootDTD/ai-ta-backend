# Plan: WU-5B3a-0 — retry-janitor FOUNDATION

**Goal:** Ship the behavior-preserving foundation the WU-5B3a drain state machine will stand on — migration 028 (dead-letter + backoff columns + partial index), the `build_rerun_inputs` shared input-builder, `KGStore.read_node_graded_at`, and `LearnerUpdateUnreconstructableError` — each independently green on real infra, with `done.py` refactored to call the builder (byte-identical).
**Architecture:** Additive Postgres column migration + ORM column adds + one new pure-ish handler module + one Neo4j read-only helper + one named error + a behavior-preserving extraction in `done.py`.
**Tech stack:** Supabase Postgres 16 + pgvector (asyncpg/SQLAlchemy async), Neo4j 5.25 (async driver), numbered SQL migrations in `database/migrations/`, pytest + Testcontainers (`pgvector:pg16`, `neo4j:5.25`).

---
provides:
  - migration `database/migrations/028_apollo_learner_janitor.sql` (5 new `apollo_problem_attempts` columns + partial index `apollo_problem_attempts_pending_idx`)
  - ORM columns on `ProblemAttempt`: `learner_update_attempts`, `learner_update_failed_at`, `learner_update_last_error`, `learner_update_next_attempt_at`, `learner_update_failed_permanently`
  - `apollo/handlers/done_inputs.py::build_rerun_inputs(db, neo, *, attempt, sess) -> RerunInputs` (+ frozen `RerunInputs` dataclass)
  - `apollo/errors.py::LearnerUpdateUnreconstructableError`
  - `apollo/knowledge_graph/store.py::KGStore.read_node_graded_at(*, attempt_id) -> dict[str, str]`
consumes:
  - `apollo/handlers/done.py` `_find_problem_payload`, `_find_problem`
  - `apollo/grading/abstention.py::min_parser_confidence_of`
  - `apollo/knowledge_graph/store.py::KGStore.read_graph`, `stamp_graded_at`
  - `apollo/persistence/models.py::ProblemAttempt`, `ApolloSession`
depends_on:
  - WU-5B2 (`feat/apollo-kg-wu5b2-negotiation-multiplier` — diff-cover base)
  - WU-5A2 (the live `done.py` shadow-chain + `run_graph_simulation`/`run_learner_update` it preserves)

---

## Overview

This is the **5B3a-0 pre-step** the WU-5B3 split proposal (`docs/superpowers/plans/2026-06-19-apollo-kg-wu5b3-split-proposal.md`, ORCHESTRATOR ADJUDICATION #6) carved out of 5B3a: the four foundation pieces of the `learner_update_pending` retry janitor that each have **independent real-infra test surface** and are **behavior-preserving** to the live Done path. It carries NO drain state machine — no claim/lease, no recompute orchestration, no dead-letter/backoff accounting, no LAYER3 interlock, no clear-flag write. Those are WU-5B3a-1.

What this unit delivers and why each piece exists:

1. **Migration 028** adds the five dead-letter/backoff columns + a partial index that 5B3a-1's claim scan and accounting will use. DDL-only here — no code reads these columns yet in this unit (the ORM maps them so a round-trip test proves the mapping; the partial index is proven to exist + be partial by the migration-chain test). Shipping the migration now de-risks 5B3a-1.
2. **`KGStore.read_node_graded_at`** reads the Neo4j `graded_at` ISO-8601 string (the durable `done_ts` source — there is NO Postgres `graded_at` column). It mirrors the existing `read_node_created_at` exactly. The janitor (5B3a-1) will parse this into a tz-aware `datetime` to anchor the belief Δt; this unit ships the read + its real-Neo4j test.
3. **`build_rerun_inputs`** is the single source of truth for re-assembling the inputs `run_graph_simulation` needs, keyed on durable sources so the LATER janitor reconstructs the correct problem (the #1 correctness guard: it MUST key on `attempt.problem_id`, never `sess.current_problem_id`). It also performs the unreconstructable pre-flight (missing `old_rubric` / missing `graded_at`) BEFORE any LLM call.
4. **`LearnerUpdateUnreconstructableError`** makes the dead-letter path a deliberate terminal state, not a caught generic crash. Mirrors `RetentionError`.
5. **`done.py` refactor** extracts its shadow-input assembly into `build_rerun_inputs` and calls it — so the live path and the future janitor share ONE builder (no drift). Behavior-preserving: the existing route tests stay green.

The grade is NEVER voided by any of this. These are read/derive helpers + a migration; the only behavior change is `done.py` sourcing its shadow inputs through the new builder instead of inline.

## Out-of-scope boundaries (READ FIRST)

**This unit does NOT (these are WU-5B3a-1 or 5B3b — do not write them here):**

- NO `apollo/handlers/learner_janitor.py`, NO `drain_pending_attempts`. The claim-LEASE + `FOR UPDATE SKIP LOCKED`, the WORK/record short-txn state machine, the dead-letter+backoff arithmetic, the LAYER3 interlock check, and the clear-flag write all belong to 5B3a-1.
- NO `apollo/learner_janitor_worker.py`, NO `Procfile` edit, NO `APOLLO_LEARNER_JANITOR_ENABLED` flag read, NO SIGTERM handler, NO opportunistic backstop in `done.py` (5B3b).
- NO edit to any frozen learner-model file: `apollo/learner_model/{belief,update,state_model,decay,negotiation,persistence}.py` and `apollo/handlers/learner_update.py` are UNTOUCHED. The clear-flag does NOT go in `run_learner_update` (that is the adjudicated 5B3a-1 decision; this unit does not pre-empt it).
- NO call to `build_rerun_inputs` from anything except `done.py` in this unit. The janitor caller is 5B3a-1.
- NO reading/writing of the five new columns from application code in this unit (other than the ORM mapping). They exist for 5B3a-1 to consume.
- NO `pool_recycle` change — already set at `database/session.py:94` (`=1800` + `pool_pre_ping` + `pool_size=10`). Do not add it.
- NO migration applied to ANY remote DB (test or prod). Testcontainers / local Docker exclusively.
- NO new packages. Stdlib + core SQLAlchemy only.

## Structural prep (from neighborhood scan)

- [ ] **None — neighborhood is clean for this unit's blast radius.** Scanned the one-ring-out change path:
  - `apollo_problem_attempts` table fan-in: the migration only ADDs columns + a partial index. No function/view/trigger reads the new columns yet (this unit ships none). The table's existing consumers (`done.py`, `next.py`, `learner_update.py`, `done_grading.py`) are not affected by additive nullable/defaulted columns.
  - `done.py` function size: `handle_done` is ~190 lines but the extraction REDUCES it (moves the ~10-line shadow-input assembly out). `build_rerun_inputs` lands as a small focused module (<60 lines). No file approaches the 800-line ceiling. No WMC/responsibility threshold tripped.
  - `KGStore` (`store.py`) is large but this unit adds one ~24-line read-only helper mirroring an existing one — no new responsibility, no coupling growth.
  - `apollo/errors.py` adds one ~12-line class mirroring `RetentionError` — no sprawl.
  - No RLS policy sprawl: Apollo tables are not RLS-scoped (course isolation is application-layer via `auth_deps.py`; see Access control section).
- Verify (fan-in sanity, run before coding): `grep -rn "learner_update_attempts\|learner_update_failed_permanently\|read_node_graded_at\|build_rerun_inputs" apollo/ database/ tests/` — expect ZERO hits (nothing references the new surface yet).

## Prior art (existing migrations + code)

Cite these exact patterns; do not invent new shapes.

- **Migration file shape + idempotent guards + LOCAL-ONLY header:** `database/migrations/027_apollo_drop_concept_cluster_id.sql` (whole file — `BEGIN;`/`COMMIT;` wrapper, `IF EXISTS` guards, the "LOCAL-DOCKER-ONLY ... NEVER applied to any remote Supabase project by an agent" header block, the "next free number" note). `database/migrations/026_apollo_learner_model.sql:13-26` for the longer DEPLOY-TIME RECONCILIATION header style. On-disk tops at 027 → **028 is next free**.
- **`learner_update_pending` column ORM style** (the exact pattern the five new columns mirror): `apollo/persistence/models.py:280-282` — `server_default=text("false")` so raw/legacy inserts see the default, `default=False` for ORM inserts, `nullable=False`. Boolean column declared with `Boolean`, comment cites the migration.
- **TIMESTAMPTZ column ORM style:** `apollo/persistence/models.py:283` (`created_at = Column(TIMESTAMP(timezone=True), ...)`) and `:423` (`last_evidence_at = Column(TIMESTAMP(timezone=True), nullable=True)`). Use `TIMESTAMP(timezone=True)` for the three TIMESTAMPTZ columns; `nullable=True` matches the DDL NULL.
- **Neo4j read-only helper to mirror EXACTLY:** `apollo/knowledge_graph/store.py:472-495` `read_node_created_at(*, attempt_id) -> dict[str, str]` — `async with self.neo.session() as s`, `MATCH (n:_KGNode {attempt_id: $aid}) RETURN n.node_id AS node_id, n.<prop> AS <prop>`, None-omit into an `out` dict. `read_node_graded_at` is the same with `graded_at` substituted for `created_at`.
- **Named error to mirror:** `apollo/errors.py:153-166` `RetentionError(ApolloError)` — `__init__(self, *, attempt_id, last_error)` keyword-only, stores attrs, `super().__init__(f"...{attempt_id}...")`. The new error follows this shape with `attempt_id` + `reason`.
- **Shadow-input assembly to extract:** `apollo/handlers/done.py:402-413` — inside `if _graph_sim_shadow_enabled():`, the `problem_payload = await _find_problem_payload(db, concept_id=sess.concept_id, problem_code=problem.id)` read + the `old_rubric=rubric` pass into `run_graph_simulation`. `_find_problem_payload` is at `done.py:229-250`; `min_parser_confidence_of` at `apollo/grading/abstention.py:70-76`.
- **The make-or-break wrong-problem evidence:** `done.py:262` derives `problem` from `sess.current_problem_id`; `next.py:94` (`sess.current_problem_id = problem.id`) advances it on Next. The durable per-attempt key is `attempt.problem_id` (Text, `models.py:270`) — the builder MUST key on it.
- **Migration-chain real-PG test harness (H2):** `tests/database/test_apollo_learner_model_migration.py` — `_chain_migrations()` content-globs every `*.sql` that matches `(CREATE TABLE|ALTER TABLE)\s+apollo_(subjects|concepts|problem_attempts)` (`:54-57`), so **028 joins the chain automatically** (it `ALTER TABLE apollo_problem_attempts`). `_apply_chain` runs them via raw asyncpg against a fresh DB on the session pgvector container (`:86-95`); `_STUB_DDL` (`:47-51`) stubs `auth.users` + `aita_search_spaces`. `mig_conn` fixture (`:120-130`) wraps each test in a rolled-back transaction.
- **Real-Neo4j helper test harness (H3):** `apollo/knowledge_graph/tests/test_store_graded_at_neo4j.py` (whole file) — uses the `tc_neo4j` fixture (`apollo/knowledge_graph/tests/conftest.py:45-66`, local Testcontainers `neo4j:5.25`, NEVER live Aura), `_StubDb` for the SQLAlchemy half (the helper never touches PG), `_write_nodes_direct` to seed nodes, NEGATIVE test attempt_ids. The new `read_node_graded_at` test reuses this exact harness + `stamp_graded_at` to set the value.
- **Behavior-preservation proof harness (H1):** `tests/database/test_done_layer3_route_postgres.py` — `db_session` savepoint fixture + LLM stubs `_adjudicator_stub`/`_auditor_stub` (`:796-801`), the `_route_neo_stubs` patch list (`:804-815`, patches `KGStore.read_graph`/`freeze`/`stamp_graded_at` + `main_chat_adjudicator`/`main_chat_auditor`), `_seed_route_session`/`_seed_route_entity` seeders, env-flag monkeypatch (`:858-859`), `handle_done(db=..., neo=object(), session_id=...)` called with a fake `neo`. The sibling `tests/database/test_done_shadow_route_postgres.py` is the shadow-flag-off byte-identity guard. BOTH must stay green after the `done.py` refactor.

## Schema changes (migration 028)

**File:** `database/migrations/028_apollo_learner_janitor.sql` (NEW). Wrap in `BEGIN;`/`COMMIT;` (027 pattern). Every column has an `IF NOT EXISTS` guard and the index has `IF NOT EXISTS` so re-applying on a partially-migrated local DB is a no-op (idempotent — required so the chain test can re-run). Include the LOCAL-DOCKER-ONLY header block from 027.

Exact DDL (copy-pasteable — these names are ADJUDICATED, do not improvise; split proposal §3 / ADJUDICATION #6):

```sql
-- 028_apollo_learner_janitor.sql  (next_free = 028; on-disk tops at 027)
-- WU-5B3a — dead-letter + exponential-backoff columns for the learner_update
-- retry janitor. NO graded_at column: the original done_ts is durably on every
-- frozen Neo4j node (store.stamp_graded_at), read back via read_node_graded_at.
-- The janitor's claim/recompute state machine (WU-5B3a-1) CONSUMES these columns;
-- WU-5B3a-0 ships the DDL + ORM mapping only (nothing reads them yet).
--
-- LOCAL-DOCKER-ONLY: applied to local Docker Postgres by the feller migration
-- tests. NEVER applied to any remote Supabase project by an agent — deploying
-- (test rehearsal, then prod) is a human/CI step (see "Deploy handoff").
--
-- Idempotent: IF NOT EXISTS guards make a re-apply on a partially-migrated local
-- DB a no-op (the chain test re-runs the whole chain into a fresh DB each session).

BEGIN;

ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_attempts           INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS learner_update_failed_at          TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_last_error         TEXT        NULL,
    ADD COLUMN IF NOT EXISTS learner_update_next_attempt_at    TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_failed_permanently BOOLEAN     NOT NULL DEFAULT false;

-- Cheap drain scan: a PARTIAL index over only the pending, not-dead rows. The
-- predicate uses ONLY the two boolean columns — `next_attempt_at <= now()` is NOT
-- in the predicate (now() is non-immutable / not index-predicate-legal) and is
-- applied as a RUNTIME query filter by the janitor (WU-5B3a-1).
CREATE INDEX IF NOT EXISTS apollo_problem_attempts_pending_idx
    ON apollo_problem_attempts (created_at)
    WHERE learner_update_pending AND NOT learner_update_failed_permanently;

COMMIT;
```

Notes:
- `learner_update_pending` already exists (migration 026, `models.py:280`). 028 does NOT touch it; the partial index references it.
- All adds are additive and safe on a populated table: the two NOT-NULL columns carry a DEFAULT so the column rewrite backfills existing rows; the three nullable columns add instantly. No backfill code needed (no 2-phase). This is NOT a destructive change — no `DROP`/`ALTER TYPE`/`NOT NULL`-without-default.

## ORM changes (ProblemAttempt)

**File:** `apollo/persistence/models.py` (EDIT). Add five `Column`s to `ProblemAttempt` (class at `:265-285`), placed AFTER the existing `learner_update_pending` block (`:280-282`) and BEFORE `created_at` (`:283`). Mirror the `learner_update_pending` style exactly: `server_default=text(...)` for the defaulted columns so raw/legacy/migration inserts see the default, `default=...` for ORM inserts, `nullable` per the DDL. Cite migration 028 in a comment.

Exact additions (note `text` and `Boolean`/`Integer`/`TIMESTAMP` are already imported at `models.py:16-29`):

```python
    # WU-5B3a janitor (migration 028): dead-letter + exponential-backoff
    # bookkeeping for the learner_update retry. WU-5B3a-0 maps these columns;
    # the drain state machine (WU-5B3a-1) reads/writes them. server_default so
    # raw-SQL/legacy/migration inserts see the default; default=... for ORM.
    learner_update_attempts = Column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    learner_update_failed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    learner_update_last_error = Column(Text, nullable=True)
    learner_update_next_attempt_at = Column(TIMESTAMP(timezone=True), nullable=True)
    learner_update_failed_permanently = Column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
```

Note on the SQLite test variant: the fast in-memory SQLite metadata tests (`Base.metadata.create_all`) build these columns from the ORM defaults — `Integer`/`Boolean`/`Text`/`TIMESTAMP` all have SQLite-native mappings, so no `with_variant` is needed (unlike `_RealArrayType`/`_JSONType`). The real-PG round-trip test (below) is the authority for column behavior.

## New error (LearnerUpdateUnreconstructableError)

**File:** `apollo/errors.py` (EDIT, +~14 lines). Add an `ApolloError` subclass mirroring `RetentionError` (`:153-166`). Keyword-only `attempt_id: int` + `reason: str`. The `reason` is one of a CLOSED set — pin it in the docstring and as a module-level tuple so the janitor (5B3a-1) and tests reference the same constant set.

```python
# Closed reason set for LearnerUpdateUnreconstructableError. The janitor's
# pre-flight (WU-5B3a build_rerun_inputs) raises with exactly one of these.
LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS = (
    "diagnostic_report_missing",  # attempt.diagnostic_report is None
    "rubric_missing",             # "rubric" absent, or rubric lacks "overall"
    "graded_at_missing",          # Neo4j read_node_graded_at returned {} (empty subgraph)
)


class LearnerUpdateUnreconstructableError(ApolloError):
    """The Done-time re-run inputs cannot be reconstructed from durable state,
    so the learner-model retry can NEVER succeed — a TERMINAL dead-letter, not a
    transient failure. Raised by `build_rerun_inputs`'s pre-flight BEFORE any LLM
    call when `attempt.diagnostic_report` is None / lacks `rubric` / the rubric
    lacks `overall` (calibration.py:88,93 dereference it unconditionally) OR the
    frozen Neo4j subgraph has no `graded_at` (empty/never-stamped → no done_ts to
    anchor the belief Δt). The janitor (WU-5B3a-1) catches this and marks the row
    `learner_update_failed_permanently` (no backoff, no LLM, never crash-loops).
    Mirrors RetentionError's shape. `reason` is one of
    LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS."""

    def __init__(self, *, attempt_id: int, reason: str) -> None:
        self.attempt_id = attempt_id
        self.reason = reason
        super().__init__(
            f"learner update unreconstructable for attempt {attempt_id}: {reason}"
        )
```

No FastAPI handler is registered in this unit — this error never reaches an HTTP boundary (it is raised inside the offline janitor path; `build_rerun_inputs` is only called by `done.py` in this unit, where the inputs are ALWAYS reconstructable because Done just wrote them, so it cannot fire on the live path). Registering a handler is a 5B3a-1/5B3b concern only if the janitor ever surfaces it.

## New Neo4j helper (KGStore.read_node_graded_at)

**File:** `apollo/knowledge_graph/store.py` (EDIT, +~24 lines). Add `read_node_graded_at` as a method on `KGStore`, placed directly AFTER `read_node_created_at` (`:472-495`). It is a near-exact mirror — same `MATCH`/`RETURN`/None-omit shape — substituting `graded_at` for `created_at`. Read-only; touches NO Postgres.

```python
    async def read_node_graded_at(self, *, attempt_id: int) -> dict[str, str]:
        """node_id -> ``graded_at`` ISO string for every ``:_KGNode`` in the
        per-attempt subgraph (WU-5B3a janitor done_ts source).

        ``read_graph`` STRIPS ``graded_at`` (``bag.pop("graded_at", None)``), and
        there is NO Postgres ``graded_at`` column — the frozen Done instant lives
        ONLY on the Neo4j nodes (``stamp_graded_at``, stored as an ISO-8601 STRING
        via ``.isoformat()``). The janitor (WU-5B3a-1) reads any one value (all
        nodes of an attempt share the same ``done_ts``) and parses it tz-AWARE via
        ``datetime.fromisoformat``. A node whose ``graded_at`` is absent
        (never-stamped / legacy) is OMITTED; an EMPTY map (``{}``) means the
        subgraph was never stamped → the janitor dead-letters
        (``graded_at_missing``). Mirrors ``read_node_created_at``; NOT a schema
        change."""
        async with self.neo.session() as s:
            result = await s.run(
                "MATCH (n:_KGNode {attempt_id: $aid}) "
                "RETURN n.node_id AS node_id, n.graded_at AS graded_at",
                aid=attempt_id,
            )
            out: dict[str, str] = {}
            async for record in result:
                graded_at = record["graded_at"]
                if graded_at is None:
                    continue
                out[record["node_id"]] = graded_at
        return out
```

The tz-aware parse itself is NOT done here (the helper returns the raw ISO string, exactly as `read_node_created_at` returns the raw `created_at`). The parse + tz-awareness assertion is the janitor's (5B3a-1). But the parse behavior is interpreter-verifiable now and IS asserted by a unit test in this plan (see `test_graded_at_parse_is_tz_aware`) so the contract the janitor depends on is locked before 5B3a-1 builds on it — `datetime.fromisoformat("2026-06-18T00:00:02+00:00")` yields a tz-aware datetime; a naive value (no offset) yields `tzinfo is None`, which would later `TypeError` on `(done_ts - last_evidence_at).days`. This test guards the SHAPE the janitor relies on (the strings `stamp_graded_at` writes carry an offset because `datetime.now(UTC).isoformat()` includes `+00:00`).

## New shared input-builder (done_inputs.build_rerun_inputs)

**File:** `apollo/handlers/done_inputs.py` (NEW, ~55 lines). The single source of truth for re-assembling the inputs `run_graph_simulation` consumes. Both `done.py` (this unit) and the janitor (5B3a-1) call it.

**Public signature (FROZEN — 5B3a-1 depends on it):**

```python
@dataclass(frozen=True)
class RerunInputs:
    problem_payload: dict
    old_rubric: dict
    student_graph: KGGraph
    parser_confidence: float
    graded_at_iso: str  # raw ISO-8601 string from Neo4j; the janitor parses it tz-aware


async def build_rerun_inputs(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    attempt: ProblemAttempt,
    sess: ApolloSession,
) -> RerunInputs:
    ...
```

**Why these fields (and only these):** `run_graph_simulation(db, neo, *, attempt, sess, student_graph, problem_payload, old_rubric)` (`done_grading.py:165-174`) needs `student_graph`, `problem_payload`, `old_rubric`. `parser_confidence` is the §6.6 MIN the janitor passes to `run_learner_update` (5B3a-1). `graded_at_iso` is the durable `done_ts` source the janitor parses tz-aware. `attempt`/`sess` are the caller's own objects, not re-fetched here. (The builder does NOT itself parse `graded_at` — it returns the raw string so the helper stays a pure read; the janitor owns the parse. This unit's tz-aware test asserts the parse contract separately.)

**Body (ground every line in real code):**

1. `problem_payload` — **MAKE-OR-BREAK: key on `attempt.problem_id`, NEVER `sess.current_problem_id`:**
   ```python
   problem_payload = await _find_problem_payload(
       db, concept_id=sess.concept_id, problem_code=attempt.problem_id,
   )
   ```
   `_find_problem_payload` lives in `done.py` (`:229-250`). To avoid a circular import (`done.py` will import from `done_inputs.py`), MOVE `_find_problem_payload` into `done_inputs.py` and have `done.py` import it from there (re-export for any other caller). `_find_problem_payload` depends only on `select`, `Concept`, `ConceptProblem` — clean to relocate. `done.py:262`'s live path derives the problem via `sess.current_problem_id` (correct at live-Done because `current_problem_id == attempt.problem_id` at that instant), but the builder is reconstructed from `attempt.problem_id` so the LATER janitor rebuilds the OLD problem even after `next.py:94` advanced `current_problem_id`. **This is the single most important correctness decision in the unit.**

2. `old_rubric` — sourced from `attempt.diagnostic_report["rubric"]`, with the dead-letter pre-flight BEFORE returning (and before any LLM call downstream):
   ```python
   report = attempt.diagnostic_report
   if report is None:
       raise LearnerUpdateUnreconstructableError(attempt_id=attempt.id, reason="diagnostic_report_missing")
   old_rubric = report.get("rubric")
   if not isinstance(old_rubric, dict) or "overall" not in old_rubric:
       raise LearnerUpdateUnreconstructableError(attempt_id=attempt.id, reason="rubric_missing")
   ```
   `diagnostic_report` is written at `done.py:335-339` as `{narrative, rubric, coverage}` (JSONB, `models.py:275`). `calibration.py:88,93` dereference `old_rubric["overall"]["letter"]`/`["overall"]["score"]` UNCONDITIONALLY — so a missing/legacy shape MUST dead-letter, never reach an LLM.

3. `student_graph` — `await KGStore(db, neo).read_graph(attempt_id=attempt.id)`. (Construct the store locally; `freeze` is PG-only so the frozen graph read == the original — split proposal §5.)

4. `graded_at_iso` — read via the new helper, dead-letter on empty:
   ```python
   graded_at_map = await KGStore(db, neo).read_node_graded_at(attempt_id=attempt.id)
   if not graded_at_map:
       raise LearnerUpdateUnreconstructableError(attempt_id=attempt.id, reason="graded_at_missing")
   graded_at_iso = next(iter(graded_at_map.values()))  # all nodes share one done_ts
   ```

5. `parser_confidence = min_parser_confidence_of(student_graph.nodes)` (`abstention.py:70`).

6. Return a frozen `RerunInputs(...)`. Immutable — no in-place mutation of `attempt`/`sess`.

**Ordering of the pre-flight checks (pin it — a test asserts it):** `diagnostic_report`/`rubric` checks come BEFORE the Neo4j `graded_at` read is NOT required, but the empty-subgraph check naturally follows the `read_graph`/`read_node_graded_at` calls. The behavioral contract a test pins: when `diagnostic_report` is None, the error reason is `diagnostic_report_missing` and NO LLM/network grading runs (the builder raises before returning, so `run_graph_simulation` is never reached). The `old_rubric` and `graded_at` checks are independent dead-letter triggers; each has its own test.

**Imports** (place at module top; all already exist in the codebase): `from dataclasses import dataclass`, `from sqlalchemy import select`, `from sqlalchemy.ext.asyncio import AsyncSession`, `from apollo.errors import LearnerUpdateUnreconstructableError`, `from apollo.grading.abstention import min_parser_confidence_of`, `from apollo.knowledge_graph.store import KGStore`, `from apollo.ontology import KGGraph`, `from apollo.persistence.models import ApolloSession, Concept, ConceptProblem, ProblemAttempt`, `from apollo.persistence.neo4j_client import Neo4jClient`. Keep the file <60 lines (small focused module per coding-style).

## done.py refactor (behavior-preserving)

**File:** `apollo/handlers/done.py` (EDIT). The refactor must be BEHAVIOR-PRESERVING — the only observable change is that the shadow `problem_payload` + `old_rubric` come through `build_rerun_inputs` instead of being assembled inline. The student-facing grade, XP, retention stamp, and ALL flag semantics are untouched.

**Constraint that makes this safe and minimal:** the live `done.py` already holds `student_graph` (`= pre_freeze_graph`, `:288`), `rubric` (computed at `:304`), and reads `problem_payload` inline at `:402-405`. `build_rerun_inputs` re-derives ALL of these. To keep the refactor a true single-source-of-truth extraction WITHOUT changing the live grade, the cleanest behavior-preserving move is:

- **Relocate `_find_problem_payload` into `done_inputs.py`** and import it back into `done.py` (`from apollo.handlers.done_inputs import _find_problem_payload`) so both modules share the one reader. `done.py`'s existing inline call site (`:402-405`) then calls the relocated function — byte-identical result.
- Inside the `if _graph_sim_shadow_enabled():` block, replace the inline `problem_payload = await _find_problem_payload(db, concept_id=sess.concept_id, problem_code=problem.id)` (`:403-405`) with a call to the shared builder that keys on `attempt.problem_id`:
  ```python
  rerun = await build_rerun_inputs(db, neo, attempt=attempt, sess=sess)
  shadow = await run_graph_simulation(
      db, neo,
      attempt=attempt,
      sess=sess,
      student_graph=student_graph,
      problem_payload=rerun.problem_payload,
      old_rubric=rubric,  # the OLD student-facing rubric (live value, unchanged)
  )
  ```

**Behavior-equivalence proof obligations (all asserted by EXISTING tests, must stay green):**
- `problem_payload`: live `done.py` keys on `problem.id` (= `_find_problem(db, sess.concept_id, sess.current_problem_id).id`, `:262`); the builder keys on `attempt.problem_id`. At LIVE Done these are EQUAL — `attempt` was found by `ProblemAttempt.problem_id == problem.id` (`:268`), so `attempt.problem_id == problem.id` by construction. Therefore the payload is identical on the live path. (The divergence only matters when the janitor runs later; that is the whole point, and a 5B3a-1 multi-problem test will prove it. This unit ALSO ships a multi-problem builder test — see test list — because the builder is the artifact, even though `done.py`'s caller can't exercise the divergence.)
- `old_rubric`: still passed as the live `rubric` value (NOT `rerun.old_rubric`) on the live path — keep it byte-identical. (We do NOT route the live `old_rubric` through the builder's `diagnostic_report` read, because at the point `done.py` calls the shadow chain, `diagnostic_report` was just committed at `:341` and equals `rubric` anyway, but using the live `rubric` keeps the diff minimal and the byte-identity guarantee trivial. The builder's `old_rubric` field is for the janitor.)
- `student_graph`: still the live `student_graph` (`pre_freeze_graph`), NOT `rerun.student_graph` — keep the live read. (The builder re-reads via `read_graph`; on the live path we already have it.)
- The `_GRAPH_SIM_LIVE`/`_GRAPH_SIM_LAYER3` blocks (`:419-442`) are UNTOUCHED.

**Net:** `done.py`'s observable behavior with every flag in its default (OFF) state stays byte-identical (`test_done_shadow_route_postgres.py` me==0/ls==0 guard); with SHADOW+LAYER3 ON the persisted state stays identical (`test_done_layer3_route_postgres.py`). The single new call is `build_rerun_inputs` supplying `problem_payload`. Add `from apollo.handlers.done_inputs import build_rerun_inputs` to the imports.

**Alternative the executor MAY take instead (allowed):** if relocating `_find_problem_payload` causes import churn the executor judges riskier than the win, keep `_find_problem_payload` in `done.py`, have `done_inputs.py` import it from `done.py` ONLY IF that does not create a circular import (it will, since `done.py` imports `build_rerun_inputs`) — so the relocation IS the recommended path. Do not duplicate the function in both modules (that violates single-source-of-truth, the entire reason this unit exists).

## Access control

**Engine:** Postgres (Supabase) — but Apollo tables do NOT use RLS for tenant isolation. This is a deliberate, pre-existing architectural choice documented in `docs/architecture/apollo.md` ("Authentication" / Key dependencies) and `docs/shared-architecture/security.md`: Apollo enforces course/user isolation at the **application layer** via `apollo/auth_deps.py` (`require_user`, `require_course_member`, `require_session_owner`) — every session-scoped route is gated and identity NEVER comes from the request body. Migration 022 (`enable_rls_stopgap`) enabled RLS broadly as a stopgap; Apollo's own access path is the FastAPI dependency gates.

**Enforcement point for the data this unit touches:** the five new columns live on `apollo_problem_attempts`, which is already reached ONLY through `apollo_sessions` (FK `session_id`, `models.py:269`) and every reader resolves the session under `require_session_owner` / `require_user`. This unit adds NO new HTTP surface and NO new query path that bypasses those gates — `build_rerun_inputs` is called only from inside `handle_done` (already owner-gated) in this unit. The columns carry operational bookkeeping (attempt counts, error strings, timestamps), not new user content, and inherit the attempt's existing isolation. **No new isolation mechanism is required; the named enforcement point is `apollo/auth_deps.require_session_owner` on the Done route.**

**Positive test (authorized path returns the row):** covered by the existing route tests — `test_done_layer3_route_postgres.py` seeds a session for `TEST_USER_ID` and `handle_done` reads/updates that user's own attempt. (No new positive query is added because no new query path is added.)

**Negative test (isolation):** not applicable as a NEW query in this unit — there is no new read/write path that could cross tenants. The attempt is only ever loaded by `session_id` under the owner gate. (The janitor's cross-user drain scoping is a 5B3a-1 concern, where a `user_id`-scoped drain + a negative test belong.)

## Public signatures (frozen API this unit ships)

These are consumed by WU-5B3a-1 — keep them stable (backward-compat is required for the stacked unit, not for any merged caller, since nothing else uses them yet):

- `apollo.errors.LearnerUpdateUnreconstructableError(*, attempt_id: int, reason: str)` + module tuple `LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS`.
- `apollo.knowledge_graph.store.KGStore.read_node_graded_at(*, attempt_id: int) -> dict[str, str]`.
- `apollo.handlers.done_inputs.RerunInputs` (frozen dataclass: `problem_payload: dict`, `old_rubric: dict`, `student_graph: KGGraph`, `parser_confidence: float`, `graded_at_iso: str`).
- `apollo.handlers.done_inputs.build_rerun_inputs(db: AsyncSession, neo: Neo4jClient, *, attempt: ProblemAttempt, sess: ApolloSession) -> RerunInputs`.
- `apollo.handlers.done_inputs._find_problem_payload(db, *, concept_id: int, problem_code: str) -> dict` (relocated from `done.py`; re-exported there).
- `ProblemAttempt` ORM gains 5 mapped columns (names exactly as in the migration).

## TDD-ordered implementation steps

Write tests FIRST (RED), then the minimal implementation (GREEN). Use `.venv/Scripts/python.exe` for ALL commands. Real-infra gates MUST run green-not-skipped (Docker is up).

1. **Error class (RED→GREEN, fastest loop).**
   - Write `apollo/errors_tests`/`apollo/persistence/tests/test_learner_update_unreconstructable_error.py` (place beside existing error/allowlist tests) asserting construction, attrs, message, and that `reason` values come from the closed tuple. Run: `.venv/Scripts/python.exe -m pytest <that file> -q` → RED.
   - Add `LearnerUpdateUnreconstructableError` + the reasons tuple to `apollo/errors.py`. → GREEN.

2. **`read_node_graded_at` (RED→GREEN, real Neo4j H3).**
   - Write `apollo/knowledge_graph/tests/test_store_graded_at_read_neo4j.py` mirroring `test_store_graded_at_neo4j.py` (uses `tc_neo4j`, `_StubDb`, `_write_nodes_direct`, NEGATIVE attempt_ids): happy path (stamp then read → the value) + empty-subgraph (`{}`) + the tz-aware parse unit assertion. Run: `.venv/Scripts/python.exe -m pytest apollo/knowledge_graph/tests/test_store_graded_at_read_neo4j.py -v` → RED (must NOT skip — Docker up).
   - Add `read_node_graded_at` to `store.py`. → GREEN.

3. **Migration 028 + ORM round-trip (RED→GREEN, real PG H2).**
   - Write `tests/database/test_apollo_learner_janitor_migration.py` (mirrors `test_apollo_learner_model_migration.py`): a migration-028-chain test (columns exist with right types/defaults; the partial index exists AND is partial) + an ORM round-trip test (insert a `ProblemAttempt`, read back the five new columns at their defaults / set values). Run: `.venv/Scripts/python.exe -m pytest tests/database/test_apollo_learner_janitor_migration.py -v` → RED (migration file absent / columns unmapped).
   - Create `database/migrations/028_apollo_learner_janitor.sql`; add the 5 ORM columns to `ProblemAttempt`. → GREEN (the migration auto-joins `_chain_migrations` via the `ALTER TABLE apollo_problem_attempts` content glob; the ORM `Base.metadata.create_all` in `_pg_url` setup picks up the new columns for the `db_session`-based round-trip).

4. **`build_rerun_inputs` (RED→GREEN, real PG + real Neo4j or stubbed Neo4j per test).**
   - Write `apollo/handlers/tests/test_build_rerun_inputs.py` (the full builder test list below). The wrong-problem multi-problem regression is MANDATORY and is the #1 guard. Run → RED.
   - Create `apollo/handlers/done_inputs.py` (relocate `_find_problem_payload`, add `RerunInputs` + `build_rerun_inputs`). → GREEN.

5. **`done.py` refactor (GREEN must STAY green — this is the regression proof).**
   - Do NOT write new tests for the refactor's behavior; the EXISTING `tests/database/test_done_shadow_route_postgres.py` + `tests/database/test_done_layer3_route_postgres.py` ARE the proof. Run them BEFORE the refactor (baseline green), apply the refactor, run them AFTER (still green). If `done.py`'s `_find_problem_payload` import moves, those tests' patch targets (`apollo.handlers.done.KGStore...`, `apollo.handlers.done_grading...`) are unaffected — `_find_problem_payload` is not patched by them. Confirm no test patches `apollo.handlers.done._find_problem_payload` (grep first; if any do, update the patch path to `apollo.handlers.done_inputs._find_problem_payload`).

6. **Owner doc** — update `docs/architecture/apollo.md` in the SAME commit (see Owner-doc updates section). Bump `last_verified` to 2026-06-19.

7. **Final gates** (Verification commands section): full `pytest apollo -q`, real-infra DB + Neo4j suites green-not-skipped, coverage + diff-cover ≥95%.

## Full test list

Every test below is REAL (no skip, no xfail, no assert-nothing). LLM/network is mocked deterministically where it would otherwise fire (the builder itself makes NO LLM call, so only the route tests stub LLMs — and those stubs already exist).

### A. Error class — `apollo/persistence/tests/test_learner_update_unreconstructable_error.py` (pure, no infra)

1. `test_error_carries_attempt_id_and_reason` — construct `LearnerUpdateUnreconstructableError(attempt_id=42, reason="rubric_missing")`; assert `.attempt_id == 42`, `.reason == "rubric_missing"`, and `str(exc)` contains `42` and `rubric_missing`. Asserts the mirror of `RetentionError`.
2. `test_is_apollo_error_subclass` — assert `issubclass(LearnerUpdateUnreconstructableError, ApolloError)` (so the NO-FALLBACK handler family / janitor catch-by-base works).
3. `test_reasons_tuple_is_the_closed_set` — assert `LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS == ("diagnostic_report_missing", "rubric_missing", "graded_at_missing")` (locks the contract the builder + janitor share).

### B. `read_node_graded_at` — `apollo/knowledge_graph/tests/test_store_graded_at_read_neo4j.py` (REAL Neo4j, `tc_neo4j`, H3) — `pytestmark = [pytest.mark.integration, pytest.mark.asyncio]`

External deps: real Testcontainers `neo4j:5.25` via `tc_neo4j`; Postgres half is `_StubDb` (helper never touches PG). NEGATIVE attempt_ids. Reuse `_write_nodes_direct` + `_eq` node builders from the sibling file's pattern.

4. `test_read_node_graded_at_returns_stamped_value` — write 2 nodes for `aid=-701`, call `store.stamp_graded_at(attempt_id=-701, ts="2026-06-18T00:00:02+00:00")`, then `read_node_graded_at(attempt_id=-701)` → a dict of 2 entries, every value == `"2026-06-18T00:00:02+00:00"`, keys == the two node_ids. Asserts the happy path the janitor relies on (all nodes share one `done_ts`).
5. `test_read_node_graded_at_empty_subgraph_returns_empty_dict` — `read_node_graded_at(attempt_id=-799)` on a never-written subgraph → `{}`. This is the `graded_at_missing` dead-letter trigger source.
6. `test_read_node_graded_at_omits_unstamped_nodes` — write 2 nodes but DO NOT stamp (so `graded_at` is absent on the nodes) → `{}` (both omitted because `graded_at is None`). Mirrors `read_node_created_at`'s null-omit behavior; proves a never-stamped node doesn't leak a `None` value.
7. `test_graded_at_parse_is_tz_aware` (pure, can live in the same file or a unit file) — assert `datetime.fromisoformat("2026-06-18T00:00:02+00:00").tzinfo is not None` AND `datetime.fromisoformat("2026-06-18T00:00:02").tzinfo is None`, and that the strings `stamp_graded_at` writes (`datetime.now(UTC).isoformat()`) always carry an offset (assert `"+00:00" in datetime.now(UTC).isoformat()`). Documents + locks the tz-aware contract the janitor's Δt subtraction depends on (a naive value would `TypeError` on `(done_ts - last_evidence_at).days`).

### C. Migration 028 + ORM — `tests/database/test_apollo_learner_janitor_migration.py` (REAL PG, H2 + H1) — `pytestmark = pytest.mark.integration`

External deps: Testcontainers `pgvector:pg16`. The chain test uses the raw-asyncpg `_pg_url` two-DB pattern from `test_apollo_learner_model_migration.py` (`_create_db` + `_apply_chain` against a fresh DB); the ORM round-trip uses the `db_session` savepoint fixture.

8. `test_migration_028_adds_five_columns` (H2, raw asyncpg on a freshly migrated DB) — apply the full chain (028 auto-joins) into a fresh DB; query `information_schema.columns` for `apollo_problem_attempts`; assert all five columns exist with the right `data_type` (`integer`, `timestamp with time zone` ×2 + the failed_at, `text`, `boolean`) and the right `is_nullable`/`column_default` (`learner_update_attempts` default `0` NOT NULL; `learner_update_failed_permanently` default `false` NOT NULL; the three others nullable, no default).
9. `test_migration_028_partial_index_exists_and_is_partial` (H2) — query `pg_indexes` / `pg_index` for `apollo_problem_attempts_pending_idx`; assert it exists, is on `apollo_problem_attempts`, and its `indpred` is non-null (it IS partial — the `WHERE learner_update_pending AND NOT learner_update_failed_permanently` predicate). Assert the indexed column is `created_at`.
10. `test_migration_028_is_idempotent` (H2) — apply 028 a SECOND time on the already-migrated DB; assert no error (the `IF NOT EXISTS` guards). Proves re-apply safety.
11. `test_problem_attempt_orm_roundtrips_new_columns` (H1, `db_session`) — seed a minimal session + `ProblemAttempt` (no values for the new cols); flush+read back; assert `learner_update_attempts == 0`, `learner_update_failed_permanently is False`, and the three nullable cols are `None` (server defaults applied). Then set `learner_update_attempts=2`, `learner_update_failed_permanently=True`, `learner_update_last_error="boom"`, `learner_update_failed_at`/`next_attempt_at` to tz-aware datetimes; commit+read back; assert exact round-trip (incl. tz preserved). Proves the ORM mapping matches the DDL.

### D. `build_rerun_inputs` — `apollo/handlers/tests/test_build_rerun_inputs.py` (REAL PG via `db_session` + Neo4j stubbed or real per test)

External deps: `db_session` (real PG savepoint) for the session/attempt/problem rows + entity. Neo4j: STUB `KGStore.read_graph` and `KGStore.read_node_graded_at` with `AsyncMock` (the assertion target is the builder's keying/sourcing/dead-letter logic, not Neo4j I/O — that is covered by section B). Seed problems via `seed_course` (mirror `test_done_layer3_route_postgres.py`'s `_INTRO`/`seed_course`). NO LLM fires (the builder makes none).

12. **`test_build_rerun_inputs_keys_on_attempt_problem_id_not_current_problem_id` (THE #1 GUARD — MANDATORY, MULTI-PROBLEM).** Seed a course with TWO problems (codes `p_a`, `p_b`). Create a session, an attempt for `p_a` (with `diagnostic_report={"rubric": {"overall": {...}}, ...}`), then ADVANCE the session: set `sess.current_problem_id = "p_b"` (simulating `next.py:94` after the student clicked Next). Stub `read_graph`→a graph, `read_node_graded_at`→`{"n1": "<iso>"}`. Call `build_rerun_inputs(db, neo, attempt=<p_a attempt>, sess=sess)`. Assert `result.problem_payload` is `p_a`'s payload (NOT `p_b`'s) — i.e. the builder rebuilt the OLD problem the pending attempt belongs to, even though `current_problem_id` now points at `p_b`. **A single-problem fixture would PASS even with the wrong key — this two-problem fixture is the only thing that catches the make-or-break bug.** Assert the payloads differ between `p_a`/`p_b` first (fixture sanity) so the test can't trivially pass.
13. `test_build_rerun_inputs_happy_path_assembles_all_fields` — single attempt with a well-formed `diagnostic_report`; stub `read_graph`→a 1-node graph (`parser_confidence=0.95`), `read_node_graded_at`→`{"n1":"2026-06-18T00:00:02+00:00"}`. Assert `RerunInputs` is frozen, `problem_payload` == the seeded payload, `old_rubric` == `diagnostic_report["rubric"]`, `student_graph` == the stubbed graph, `parser_confidence == 0.95` (= `min_parser_confidence_of`), `graded_at_iso == "2026-06-18T00:00:02+00:00"`.
14. `test_build_rerun_inputs_dead_letters_when_diagnostic_report_none` — attempt with `diagnostic_report=None`; assert `build_rerun_inputs` raises `LearnerUpdateUnreconstructableError` with `reason="diagnostic_report_missing"`, and assert (via an `AsyncMock` spy on `read_graph`/the absence of any grading call) that NO LLM/grading is reached — the raise happens at the pre-flight. (Use `pytest.raises` and assert `.reason`.)
15. `test_build_rerun_inputs_dead_letters_when_rubric_missing` — `diagnostic_report={"narrative": "x", "coverage": {}}` (no `"rubric"` key) → raises with `reason="rubric_missing"`. Plus a variant: `diagnostic_report={"rubric": {"no_overall": 1}}` (rubric present but lacks `"overall"`) → also `reason="rubric_missing"` (guards the `calibration.py:88,93` unconditional `["overall"]` deref).
16. `test_build_rerun_inputs_dead_letters_when_graded_at_empty` — well-formed `diagnostic_report`, but stub `read_node_graded_at`→`{}` (empty subgraph) → raises with `reason="graded_at_missing"`. The empty-Neo4j-subgraph terminal trigger.
17. `test_find_problem_payload_relocated_importable_from_both_modules` — assert `from apollo.handlers.done_inputs import _find_problem_payload` works AND `from apollo.handlers.done import _find_problem_payload` still works (the re-export), and they are the SAME object (`is`). Guards the single-source-of-truth relocation (no duplicate definition).

### E. Behavior-preservation (EXISTING tests, must STAY green — no new tests)

18. `tests/database/test_done_shadow_route_postgres.py` — the shadow-flag-OFF byte-identity guard (me==0/ls==0). Run before + after the `done.py` refactor; must be unchanged-green. This proves the refactor did not alter the default-state behavior.
19. `tests/database/test_done_layer3_route_postgres.py` — the SHADOW+LAYER3-ON route (incl. `test_done_ts_single_instant`). Run before + after; must be unchanged-green. This proves the shadow chain still receives an identical `problem_payload` through the builder.

(If a pre-existing test patches `apollo.handlers.done._find_problem_payload` — grep to confirm; none observed — repoint it to `apollo.handlers.done_inputs._find_problem_payload`. This is the only test-edit the refactor may require, and it is a patch-target rename, not a behavior change.)

## Migration plan (forward-only)

Single migration, additive, non-destructive — no 2-phase needed (all adds are nullable or defaulted; the two NOT-NULL columns carry a DEFAULT so the table rewrite backfills existing rows in one statement). On a large table the column adds with a constant `DEFAULT` are metadata-only in PG11+ (no full rewrite), and the partial-index build takes a brief `SHARE` lock proportional to the pending-row count (tiny — the predicate matches only un-cleared rows). No `NOT VALID`/`VALIDATE` dance is needed because there are no new CHECK/FK constraints.

- [ ] **Migration file:** `database/migrations/028_apollo_learner_janitor.sql`
  - Contents: the DDL in "Schema changes" above (5 `ADD COLUMN IF NOT EXISTS` + 1 `CREATE INDEX IF NOT EXISTS`, wrapped in `BEGIN;`/`COMMIT;`).
  - Verify (local, via the chain test): `.venv/Scripts/python.exe -m pytest tests/database/test_apollo_learner_janitor_migration.py -v` — expect the column-existence + partial-index + idempotency + ORM round-trip tests GREEN-not-skipped.
  - There is NO Phase 2. This is the only migration in this unit. (The columns are CONSUMED by 5B3a-1, but no further DDL is needed there either.)

## Rollback plan

Include this rollback block as a comment at the BOTTOM of `028_apollo_learner_janitor.sql` (the repo has no `down.sql` convention — 027 carries none; an inline comment block is the convention here). The forward migration is idempotent so a re-apply is safe; the rollback is for a deliberate revert during local rehearsal.

```sql
-- Rollback (LOCAL ONLY — never run against a remote DB):
-- DROP INDEX IF EXISTS apollo_problem_attempts_pending_idx;
-- ALTER TABLE apollo_problem_attempts
--     DROP COLUMN IF EXISTS learner_update_attempts,
--     DROP COLUMN IF EXISTS learner_update_failed_at,
--     DROP COLUMN IF EXISTS learner_update_last_error,
--     DROP COLUMN IF EXISTS learner_update_next_attempt_at,
--     DROP COLUMN IF EXISTS learner_update_failed_permanently;
```

- ORM rollback: revert the five `Column` adds on `ProblemAttempt` (a git revert of the `models.py` edit).
- `done_inputs.py`, the error class, and `read_node_graded_at` are purely additive — rollback is deleting the new file / reverting the additive edits; nothing else references them yet.
- `done.py` refactor rollback: git-revert to inline the `problem_payload` read and re-inline `_find_problem_payload`. The route tests are the safety net either way.

## Verification commands (ALL local — executor runs these)

Use `.venv/Scripts/python.exe` (py3.12) for ALL commands — bare `python` is anaconda-base and lacks deps (an ImportError from bare python is NOT a blocker; switch interpreters). Docker is up (Testcontainers `pgvector:pg16` + `neo4j:5.25`). The real-infra gates MUST run green-not-skipped.

- [ ] Fan-in sanity (before coding): `grep -rn "learner_update_attempts\|read_node_graded_at\|build_rerun_inputs" apollo/ database/ tests/` — expect zero hits initially.
- [ ] **Real-PG migration gate (MANDATORY, green-not-skipped):** `.venv/Scripts/python.exe -m pytest tests/database/test_apollo_learner_janitor_migration.py -v` — expect: migration-028 chain test + partial-index test + idempotency + ORM round-trip all PASS (NOT skipped). This is the changed-`database/migrations/028_*.sql` + `tests/database/*` gate.
- [ ] **Real-Neo4j store gate (MANDATORY, green-not-skipped):** `.venv/Scripts/python.exe -m pytest apollo/knowledge_graph/tests/test_store_graded_at_read_neo4j.py -v` — expect: happy-path + empty-subgraph + omit-unstamped + tz-aware-parse all PASS (NOT skipped). This is the changed-`apollo/knowledge_graph/store.py` gate.
- [ ] Error + builder unit/integration: `.venv/Scripts/python.exe -m pytest apollo/persistence/tests/test_learner_update_unreconstructable_error.py apollo/handlers/tests/test_build_rerun_inputs.py -v` — expect all PASS, including the multi-problem wrong-problem regression (test #12).
- [ ] **Behavior-preservation (run BEFORE and AFTER the `done.py` refactor):** `.venv/Scripts/python.exe -m pytest tests/database/test_done_shadow_route_postgres.py tests/database/test_done_layer3_route_postgres.py -v` — expect IDENTICAL green both times (the refactor changed nothing observable).
- [ ] **No regressions across Apollo:** `.venv/Scripts/python.exe -m pytest apollo -q` — expect green.
- [ ] **Coverage + patch gate:** `.venv/Scripts/python.exe -m pytest --cov=apollo --cov-report=xml -q` then `.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml --compare-branch=feat/apollo-kg-wu5b2-negotiation-multiplier --fail-under=95` (or the project's `diff-cover` entrypoint) — expect ≥95% patch coverage on changed lines vs `feat/apollo-kg-wu5b2-negotiation-multiplier`.
- [ ] Lint (if wired): `.venv/Scripts/python.exe -m ruff check apollo/handlers/done_inputs.py apollo/errors.py apollo/knowledge_graph/store.py apollo/persistence/models.py apollo/handlers/done.py` — expect no new warnings.
- [ ] **STOP here.** No deploy command, no remote migration apply. The executor's job ends at local green.

## Deploy handoff (HUMAN/CI only — never executed by feller)

Migration 028 is applied to remote Supabase ONLY by a human/CI step, NEVER by a feller agent. When the time comes (NOT part of this unit's execution):

1. Verify the target project's applied migration set first. Per project memory, the test project (`hjevtxdtrkxjcaaexdxt`) historically lags (024/025 not applied as of 2026-06-15; the 023 two-file collision). Confirm 026 + 027 are present before applying 028; reconcile numbering if the remote set diverges.
2. **Rehearse on the TEST Supabase project first** (`supabase-test` MCP / Railway staging backend targets the TEST project), then — as a separate, human-gated step — apply to PROD (`uduxdniieeqbljtwocxy`).
3. Run `get_advisors` (security + performance) after applying — confirm the partial index is recognized and no new lints fire.
4. Post-apply sanity (against real data, read-only): `SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name='apollo_problem_attempts' AND column_name LIKE 'learner_update_%';` and confirm `apollo_problem_attempts_pending_idx` exists in `pg_indexes`.
5. The new columns are dormant until WU-5B3a-1 + 5B3b ship and `APOLLO_LEARNER_JANITOR_ENABLED` is flipped — applying 028 alone changes NO runtime behavior.

## Owner-doc updates (drift contract)

Owner doc: `docs/architecture/apollo.md` (`owns: apollo/**`). Update in the SAME commit; set `last_verified: 2026-06-19` (line 13).

1. **Module map — `apollo/handlers/` row (`:33`):** add a sentence registering `done_inputs.py`: "**WU-5B3a-0** adds `done_inputs.py` (`build_rerun_inputs(db, neo, *, attempt, sess) -> RerunInputs`) — the single source of truth for re-assembling `run_graph_simulation` inputs, keyed on **`attempt.problem_id`** (NEVER `sess.current_problem_id` — the durable per-attempt key, so the WU-5B3a janitor reconstructs the OLD problem even after `next.py` advanced the session), sourcing `old_rubric` from `attempt.diagnostic_report['rubric']` and the `done_ts` from Neo4j `graded_at`; it raises `LearnerUpdateUnreconstructableError` (terminal dead-letter, no LLM) when `diagnostic_report` is None / lacks `rubric`/`overall` or the Neo4j subgraph has no `graded_at`. `_find_problem_payload` RELOCATED here from `done.py` (re-exported there); `done.py`'s shadow chain now sources `problem_payload` through `build_rerun_inputs` (behavior-preserving — the route tests stay green)."
2. **Module map — `apollo/knowledge_graph/` row (`:36`):** extend the `read_node_created_at` sentence: "**WU-5B3a-0** adds the sibling `read_node_graded_at(*, attempt_id) -> dict[node_id, graded_at_iso]` (mirrors `read_node_created_at`; None-omit; `{}` on an unstamped/empty subgraph) — the durable `done_ts` source for the janitor (there is NO Postgres `graded_at` column); the janitor parses it tz-aware via `datetime.fromisoformat`."
3. **Module map — `apollo/persistence/` row (`:44`) OR the `ProblemAttempt` mention:** note the five new columns: "**WU-5B3a-0 (migration 028)** adds `apollo_problem_attempts.learner_update_{attempts,failed_at,last_error,next_attempt_at,failed_permanently}` (dead-letter + exponential-backoff bookkeeping for the learner_update retry janitor) + the partial index `apollo_problem_attempts_pending_idx ON (created_at) WHERE learner_update_pending AND NOT learner_update_failed_permanently`. WU-5B3a-0 ships the DDL + ORM mapping only; the drain state machine (WU-5B3a-1) consumes them. No `graded_at` column — `done_ts` lives on Neo4j."
4. **`apollo/` root row (`:32`) errors note:** add `LearnerUpdateUnreconstructableError` to the named-error inventory: "**WU-5B3a-0** adds `LearnerUpdateUnreconstructableError(*, attempt_id, reason)` (mirrors `RetentionError`) — the terminal dead-letter the retry janitor raises when re-run inputs are unreconstructable; `reason ∈ {diagnostic_report_missing, rubric_missing, graded_at_missing}`. Not HTTP-registered (offline janitor path)."
5. **Main data flows — step 8 (`:121`):** append a sentence: "**WU-5B3a-0** extracts the shadow-input assembly into `done_inputs.build_rerun_inputs` (shared with the future janitor); `done.py` now sources the shadow `problem_payload` through it (keyed on `attempt.problem_id`), behavior-preserving with all flags OFF."
6. **Migration baseline:** anywhere migrations are enumerated (Key dependencies `:132` mentions migrations by number), note 028 as the next on-disk migration after 027.

Keep edits additive — do not delete existing WU-4C/5A/5B prose. Bump `last_verified` to 2026-06-19.

## Downstream consumers

Grep the app source for anything that queries the affected table / would be impacted:

```bash
grep -rn "apollo_problem_attempts\|ProblemAttempt\|learner_update_pending" apollo/ database/ scripts/
grep -rn "_find_problem_payload\|read_node_created_at\|read_graph" apollo/
grep -rn "apollo.handlers.done import\|from apollo.handlers.done " apollo/ tests/
```

Expected consumers and the impact:
- **`apollo/handlers/done.py`** — the only caller of `build_rerun_inputs` in this unit; refactored, proven by route tests.
- **`apollo/handlers/next.py`** (`:94`), **`learner_update.py`**, **`done_grading.py`** — read/write `apollo_problem_attempts` but NOT the new columns; additive columns are transparent to them.
- **`tests/database/test_apollo_learner_model_migration.py`** — its `_chain_migrations()` content-glob will START including 028 (matches `ALTER TABLE apollo_problem_attempts`). This is BENIGN: 028 is idempotent and additive, so the 026 chain test still builds a valid schema with 028 appended. Confirm that suite stays green (run it).
- **WU-5B3a-1 (future, NOT this unit)** — the real consumer of all five columns + `build_rerun_inputs` + `read_node_graded_at`. The frozen signatures section is its contract.
- No service/RPC/view/Edge-Function references these columns (they did not exist before this unit).

## Risks

- **[HIGH — make-or-break] Wrong-problem re-derivation if the builder keys on `current_problem_id`.** `done.py:262` derives the problem from `sess.current_problem_id`; `next.py:94` advances it. The builder MUST key `problem_payload` on `attempt.problem_id`. Invisible in single-problem fixtures. *Mitigation:* the MANDATORY multi-problem regression test (#12) with a session advanced past the pending attempt's problem; the fixture asserts `p_a`/`p_b` payloads differ first so it can't trivially pass. Confidence the plan catches it: HIGH (the test is the guard).
- **[HIGH] `old_rubric` hard-required downstream.** `calibration.py:88,93` dereference `old_rubric["overall"]["letter"]`/`["score"]` UNCONDITIONALLY. A None/legacy `diagnostic_report` would `KeyError`/`TypeError` if it ever reached calibration. *Mitigation:* the pre-flight dead-letters BEFORE returning (reasons `diagnostic_report_missing`/`rubric_missing`, incl. the `"overall"`-absent variant). Tests #14/#15. Confidence: HIGH.
- **[MEDIUM] tz-aware `done_ts` contract.** The janitor will compute `(done_ts - last_evidence_at).days`; a naive `done_ts` `TypeError`s. This unit returns the raw ISO string (correct — the parse is the janitor's), but the contract is fragile if `stamp_graded_at` ever wrote a naive value. *Mitigation:* `stamp_graded_at` normalizes via `datetime.isoformat()` and the live `done_ts = datetime.now(UTC)` always carries `+00:00`; test #7 locks the tz-aware + naive-failure shapes. Confidence: HIGH that the string carries an offset.
- **[MEDIUM] `done.py` refactor accidentally changes the live grade.** The extraction must keep `student_graph`/`old_rubric` as the LIVE values and only source `problem_payload` through the builder. *Mitigation:* the diff is ~5 lines; the existing route tests (`test_done_shadow_route_postgres.py` byte-identity guard + `test_done_layer3_route_postgres.py`) are the regression proof, run before+after. Confidence: HIGH.
- **[MEDIUM] Circular import between `done.py` and `done_inputs.py`.** `done.py` imports `build_rerun_inputs`; `done_inputs.py` must NOT import from `done.py`. *Mitigation:* relocate `_find_problem_payload` INTO `done_inputs.py` (it depends only on `select`/`Concept`/`ConceptProblem`); `done.py` imports it back. The plan specifies this explicitly. Confidence: HIGH.
- **[LOW] Migration 028 auto-joining the 026 chain test.** The content glob pulls 028 into `test_apollo_learner_model_migration.py`'s chain. *Mitigation:* 028 is additive + idempotent; verify that suite stays green. Lock duration on a populated table: column adds with constant DEFAULT are metadata-only (PG11+); the partial index build is proportional to pending-row count (tiny). On the empty local test DB it's instant. Confidence: HIGH.
- **[LOW] Coverage of the dead-letter branches.** Three independent raise paths must each be hit for ≥95% patch coverage. *Mitigation:* tests #14/#15/#16 cover each `reason` branch; #13 covers the happy return. Confidence: HIGH.
- **[LOW] Existing test patches `apollo.handlers.done._find_problem_payload`.** Would break on relocation. *Mitigation:* grep first; none observed in the route tests (they patch `KGStore`/`main_chat_*`). If found, repoint the patch target. Confidence: MEDIUM (grep is the check).

## Deviations I'd allow the executor

- **Test file placement/naming.** Exact filenames are suggestions; place each test beside its peers (error test near `apollo/persistence/tests/` allowlist tests; store test in `apollo/knowledge_graph/tests/`; builder test in `apollo/handlers/tests/`; migration test in `tests/database/`). Keep the test NAMES (they encode the assertion contract) and the harness shape (H1/H2/H3) as specified.
- **Whether `read_node_graded_at` returns the raw string vs a parsed datetime.** The plan returns the raw string (mirrors `read_node_created_at`, keeps the parse with the janitor). If the executor has a strong reason to parse in the helper, that is allowed ONLY IF it returns tz-aware datetimes and a test asserts tz-awareness — but the raw-string mirror is strongly preferred for symmetry and is what 5B3a-1 expects.
- **The `done.py` extraction mechanics** (which exact lines move) — allowed to vary as long as: (a) `_find_problem_payload` is NOT duplicated, (b) the live `student_graph`/`old_rubric` stay the live values, (c) `problem_payload` flows through `build_rerun_inputs` keyed on `attempt.problem_id`, and (d) both route tests stay byte-identical green.
- **Adding extra defensive tests** beyond the listed set (e.g. an additional `RerunInputs` immutability assertion) — encouraged, never at the cost of the mandatory ones.

NOT allowed to deviate: keying the builder on `attempt.problem_id` (never `current_problem_id`); the five exact column names + the index name; sourcing `old_rubric` from `diagnostic_report["rubric"]`; the three dead-letter reasons; real-infra gates running green-not-skipped; no remote migration apply; no new packages; no edits to frozen learner-model files or `learner_update.py`; no drain/worker/Procfile code.
