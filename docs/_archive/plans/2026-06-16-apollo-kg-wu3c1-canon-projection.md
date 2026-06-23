# Plan: WU-3C1 — :Canon projection seeder + retention change + named errors

**Goal:** Project Layer-1 Postgres entities into idempotent Neo4j `:Canon` nodes keyed on the surrogate entity id, add Layer-2 node scoping/timestamp properties (`user_id`/`search_space_id`/`created_at`/`graded_at`) + a `user_id` index, and flip retention so `handle_end` persists (no delete) while `restart_problem` still wipes — all guarded by new NO-FALLBACK named errors.
**Architecture:** Python 3.12 / FastAPI backend. Reads Postgres (`apollo_kg_entities` via SQLAlchemy async) and writes Neo4j (async `neo4j` driver via `Neo4jClient`/`KGStore`). No HTTP surface change; no new migration.
**Tech stack:** SQLAlchemy async + asyncpg, neo4j async driver, pytest + pytest-asyncio + Testcontainers (pgvector `pg16` + `neo4j:5.25`), diff-cover patch gate.

---
provides:
  - `apollo/knowledge_graph/canon_projection.py` — `CanonNodeSpec` (pure entity→property mapping), `project_canon(db, neo, *, concept_id|search_space_id)` idempotent seeder
  - `scripts/seed_canon_projection.py` — CLI entrypoint wrapping `project_canon` (operational rebuild)
  - `KGStore.write_nodes(...)` extended scoping kwargs (`user_id`, `search_space_id`) carried as Layer-2 node props + `created_at`
  - `KGStore.stamp_graded_at(attempt_id)` — Done-time `graded_at` helper
  - new Neo4j `:Canon` label + `key`/`user_id` schema (constraint + indexes) for WU-3C2 resolver to target
  - new named errors `CanonProjectionError`, `RetentionError` in `apollo/errors.py`
consumes:
  - `apollo_kg_entities` (id, concept_id, canonical_key, kind, display_name) — seeded by WU-3B
  - `apollo_concepts` → `apollo_subjects.search_space_id` (course scope join)
  - Neo4j target graph (Testcontainers in tests, Aura in prod)
  - `AuthContext` user_id/search_space_id (server-injected at write call sites)
depends_on:
  - WU-3A migration 026 (`apollo_kg_entities` schema) — already applied in test harness, never remote
  - WU-3B bernoulli seed (`scripts/seed_apollo_learner_model.py`) — provides the entity rows the projection reads
  - base branch for diff-cover: `feat/apollo-kg-wu3b-bernoulli-seed`
---

## Overview

WU-3C1 lands three independent-but-related pieces, all grounded in the architecture decision
doc (`docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`):

1. **:Canon projection (§2 Neo4j subsection).** A rebuildable, idempotent seeder reads Layer-1
   entities from Postgres (`apollo_kg_entities`, seeded by WU-3B) and `MERGE`s `:Canon` nodes in
   Neo4j keyed on `key = apollo_kg_entities.id` (the BIGSERIAL surrogate id). The surrogate-id key
   is **load-bearing**: Postgres uniqueness is `(concept_id, canonical_key)`, so a key synthesized
   from `search_space_id:canonical_key` would fuse two different concepts that both mint the same
   `canonical_key` (e.g. two fluid concepts both minting `eq.continuity`) into ONE node and
   cross-contaminate every future `RESOLVES_TO` edge. The entity id makes fusion impossible and
   survives key renames. Carried properties: `search_space_id`, `concept_id`, `canonical_key`,
   `kind`, `display_name`. Misconception entities (`kind = "misconception"`, `misc.*` keys) ARE
   projected — they are competing resolution targets in WU-3C2. Never authored in Neo4j; sidesteps
   dual-write drift.

2. **Layer-2 node scoping/timestamp schema (§2 Neo4j subsection).** `KGStore.write_nodes` gains
   server-injected `user_id` (opaque UUID) + `search_space_id` properties and a `created_at`
   timestamp on every Layer-2 node; a new `stamp_graded_at` helper sets `graded_at` at Done. A new
   Neo4j index on `:_KGNode(user_id)`; the existing `attempt_id` index stays.

3. **Retention change (§7).** `handle_end` STOPS deleting per-attempt subgraphs (persist instead);
   `done.py` stamps `graded_at` on the frozen graph; `restart_problem` KEEPS deleting (explicit
   student wipe); `delete_subgraph` remains in `KGStore` as the future janitor's primitive; the
   `attempt_id < 0` test-cleanup convention is untouched.

All infra-failure paths raise NO-FALLBACK named errors (§5 mechanics). **Out of scope:** the §5
resolver, `RESOLVES_TO` edges, and the resolution node-fields — those are WU-3C2.

## Prior art (sibling modules)

- **Pure conversion core + DB seeder split** — `apollo/persistence/learner_model_seed.py` (frozen
  `EntitySpec` dataclass, DB-free pure functions, `SeedError(RuntimeError)` named error) + DB write
  layer in `scripts/seed_apollo_learner_model.py:165` (`_upsert_entity` idempotent on
  `(concept_id, canonical_key)`). WU-3C1 mirrors this split: a pure `CanonNodeSpec` mapping in
  `canon_projection.py` + an async seeder + a thin `scripts/` CLI.
- **Neo4j write/read via per-label Cypher templates** — `apollo/knowledge_graph/store.py:54-73`
  (`_NODE_CREATE_CYPHER` / `_EDGE_CREATE_CYPHER` built per label because Cypher can't parameterize
  labels), `:_KGNode` secondary label convention (store.py:55), `None`-property omission
  (store.py:103). The `:Canon` MERGE follows the same single-label, `UNWIND $rows` batch shape but
  uses `MERGE` (not `CREATE`) on `key`.
- **Neo4j integration test pattern** — `apollo/knowledge_graph/tests/test_store_neo4j.py:37`
  (`neo4j_client` fixture, NEGATIVE attempt_ids, direct-session writes bypassing Postgres freeze).
  `tests/conftest.py:228` provides `neo4j_client` (Testcontainers `neo4j:5.25`, wiped per test) and
  `tests/conftest.py:162` provides `db_session` (pgvector, savepoint-rollback). The seeder test
  needs BOTH — Postgres source + Neo4j target.
- **Real-Postgres seed test harness** — `tests/database/test_seed_apollo_learner_model.py:151`
  (`seeded_db` builder: fresh migrated DB per test, `_seed_bernoulli_curriculum` inserts
  subject/concept/problems, `_seed_two_courses`-style helpers). The two-concept fusion-isolation
  test reuses this exact harness to seed two concepts each minting `eq.continuity`.
- **Named-error NO-FALLBACK convention** — `apollo/errors.py` (`ApolloError` base, one class per
  failure, `CoverageGradingError` for "grading unavailable, try again" instead of silent downgrade).
- **Neo4j schema constraints/indexes** — `apollo/persistence/neo4j_schema.cypher` (per-label
  `CREATE CONSTRAINT … IF NOT EXISTS`, `CREATE INDEX … IF NOT EXISTS` on `_KGNode(attempt_id)`).
  WU-3C1 appends a `:Canon(key)` uniqueness constraint + `:_KGNode(user_id)` index here.

## Structural prep (from neighborhood scan)

Change-path files scanned (imports = CBO proxy, public methods = WMC proxy):

| File | Imports | Public methods/fns | Verdict |
|---|---|---|---|
| `apollo/knowledge_graph/store.py` (683 lines, edit) | ~9 | `KGStore`: 13 methods | **Flag: imports just over 8, file approaching size budget.** Adding 1 method + scoping kwargs keeps it < 800 lines. No extraction required — see note. |
| `apollo/handlers/lifecycle.py` (115 lines, edit) | 5 | 3 handlers | Clean |
| `apollo/handlers/done.py` (317 lines, edit) | ~14 | 1 public handler + helpers | Imports high but pre-existing; WU-3C1 adds one `stamp_graded_at` call, no new import beyond what store re-exports. Acceptable. |
| `apollo/handlers/restart_problem.py` (72 lines, edit — assert-only) | 6 | 1 handler | Clean |
| `apollo/errors.py` (136 lines, edit) | 0 | 11 classes | Clean |
| `apollo/knowledge_graph/canon_projection.py` (NEW) | target ~6 | pure fns + 1 seeder | New, keep < 250 lines |

- [ ] **No extraction required.** `store.py` is the only flagged file (9 imports, 683 lines). WU-3C1
  adds `stamp_graded_at` (~12 lines) and two optional kwargs to `write_nodes` — net well under the
  800-line budget. The `:Canon` projection deliberately lives in its OWN new module
  (`canon_projection.py`), NOT bolted onto `store.py`, precisely to avoid pushing store.py over
  budget and to keep the projection (a batch/operational concern) decoupled from per-request CRUD.
- [ ] No circular-import risk: `canon_projection.py` imports `Neo4jClient` + `KGEntity` + the new
  errors; nothing imports `canon_projection` except the CLI and tests (fan-in 2).
- Verify: `python -c "import apollo.knowledge_graph.canon_projection"` and `wc -l apollo/knowledge_graph/store.py` < 800.

Budget check: structural prep is 0 real steps (just the decision to put the new code in a new
module). Well under the 30% threshold.

## Layered tasks (ORDER MATTERS)

> TDD: for each code task, write the listed tests in the **Full test list** section FIRST (RED),
> then implement to GREEN. The task order below is the implementation/build order; the test for
> each step is named alongside it and is authored before the code for that step.

### 1. DB migration

- [ ] **NONE — no Postgres migration in WU-3C1.** The `apollo_kg_entities` schema (id surrogate PK,
  concept_id, canonical_key, kind, display_name, payload, aliases) already exists from WU-3A
  migration 026 (`database/migrations/026_apollo_learner_model.sql`) and is populated by WU-3B. The
  `:Canon` projection READS those rows; it adds no columns. The Layer-2 scoping properties live in
  **Neo4j**, not Postgres, so they are schema changes to `neo4j_schema.cypher` (task 8), not a
  numbered SQL migration. **Binding constraint honored:** agents never apply migrations to any
  remote DB — and here there is no migration to apply at all.
- Justification for absence (per layered_task_order rule): all Postgres schema this unit needs is
  pre-existing; the only new schema is Neo4j labels/indexes, covered in task 8.
- Verify: `grep -rn "ALTER TABLE\|CREATE TABLE" database/migrations/` shows no new file added by this unit.

### 2. Named errors (apollo/errors.py)

- [ ] File: `apollo/errors.py` (edit — append two classes after `CoverageGradingError`)
- Add `CanonProjectionError(ApolloError)` — raised when the `:Canon` seeder hits an infrastructure
  failure (Neo4j unreachable / write fails / Postgres read fails mid-projection). Carries
  `stage: str` and `last_error: str`, message `f"Canon projection failed at stage {stage!r}: {last_error}"`.
  NO-FALLBACK: a failed projection must surface, never silently leave a partial graph that grading
  would later read as authoritative.
- Add `RetentionError(ApolloError)` — raised when a retention-critical write fails in a way that must
  NOT be swallowed: specifically `stamp_graded_at` failing at Done (the frozen graph must carry
  `graded_at` or the janitor/Layer-3 timeline is wrong). Carries `attempt_id: int` and `last_error: str`,
  message `f"Retention operation failed for attempt {attempt_id}: {last_error}"`.
- **Do NOT register HTTP handlers for these in `api.py` in this unit** — the projection runs as an
  operational script (CLI), not a request handler, and `stamp_graded_at` failing inside `handle_done`
  surfaces through the existing Done error path. (api.py is OUT of the WU-3C1 scope file list; adding
  a handler there would breach scope.) Document this choice in Error contract decisions.
- Verify: `python -c "from apollo.errors import CanonProjectionError, RetentionError"`; the two
  classes subclass `ApolloError`.

### 3. :Canon projection — pure mapping core (canon_projection.py)

- [ ] File: `apollo/knowledge_graph/canon_projection.py` (NEW)
- Add frozen dataclass `CanonNodeSpec` — the pre-Neo4j property bag for one `:Canon` node:
  `key: int` (== entity.id), `search_space_id: int`, `concept_id: int`, `canonical_key: str`,
  `kind: str`, `display_name: str`. `@dataclass(frozen=True)` (immutability rule).
- Add pure fn `entity_to_canon_spec(entity_id, *, concept_id, search_space_id, canonical_key, kind, display_name) -> CanonNodeSpec`
  — the single mapping point Postgres-entity → `:Canon` property bag. Pure, DB-free, no Neo4j.
  Tested without any container (fast unit).
- Add pure fn `canon_spec_to_row(spec: CanonNodeSpec) -> dict[str, Any]` — flattens the spec to the
  Cypher `$rows` property bag (`{key, search_space_id, concept_id, canonical_key, kind, display_name}`),
  the exact shape the MERGE template consumes. Pure.
- Add the Cypher constant `_CANON_MERGE_CYPHER` — `UNWIND $rows AS row MERGE (c:Canon {key: row.key})
  SET c.search_space_id = row.search_space_id, c.concept_id = row.concept_id,
  c.canonical_key = row.canonical_key, c.kind = row.kind, c.display_name = row.display_name
  RETURN count(c) AS merged`. MERGE on `key` ONLY (the surrogate id) is what makes re-runs
  idempotent and fusion impossible.
- Why `key` is the surrogate id, restated in a module docstring comment for the next reader (the
  load-bearing isolation guarantee, §2 Neo4j subsection).
- Verify: `python -c "import apollo.knowledge_graph.canon_projection"`; fast unit tests
  (`test_canon_projection_mapping.py`) pass with no Docker.

### 4. :Canon projection — DB→Neo4j seeder (canon_projection.py)

- [ ] File: `apollo/knowledge_graph/canon_projection.py` (same NEW file)
- Add async fn `load_entity_specs(db: AsyncSession, *, search_space_id: int | None = None,
  concept_id: int | None = None) -> list[CanonNodeSpec]` — reads `apollo_kg_entities` joined to
  `apollo_concepts` → `apollo_subjects` to recover `search_space_id`. Scope selector: if
  `concept_id` given, filter to that concept; elif `search_space_id` given, filter to all concepts
  under that course's subjects; else raise `ValueError` (refuse a global unscoped projection — the
  isolation invariant forbids a course-blind seed). Returns one `CanonNodeSpec` per entity row,
  including `kind = "misconception"` rows (no kind filtering). Wrap the DB read in try/except →
  raise `CanonProjectionError(stage="load_entities", last_error=str(exc))` on failure.
- Add async fn `project_canon(db: AsyncSession, neo: Neo4jClient, *, search_space_id: int | None = None,
  concept_id: int | None = None) -> CanonProjectionResult` — the seeder:
  1. `specs = await load_entity_specs(db, ...)`.
  2. `rows = [canon_spec_to_row(s) for s in specs]`.
  3. One Neo4j session, run `_CANON_MERGE_CYPHER` with `rows=rows`; read `merged` count.
  4. Wrap the Neo4j write in try/except → `CanonProjectionError(stage="merge_canon", last_error=str(exc))`.
  5. Return frozen `CanonProjectionResult(merged=int, entity_count=len(specs))`.
- Add frozen dataclass `CanonProjectionResult` (`merged: int`, `entity_count: int`).
- **Idempotency contract:** MERGE on `key` means a second `project_canon` run over the same entities
  yields the SAME node count (SET re-applies identical properties; no duplicates). This is asserted
  in the integration test.
- **No `RESOLVES_TO`, no resolution fields, no `:Canon` reads** — projection is write-only here
  (WU-3C2 owns reads/edges).
- Verify: integration test `test_canon_projection_seeder.py` green (needs db_session + neo4j_client).

### 5. :Canon projection — CLI entrypoint (scripts/seed_canon_projection.py)

- [ ] File: `scripts/seed_canon_projection.py` (NEW — mirrors `scripts/seed_apollo_learner_model.py`)
- `async def run(database_url: str, *, search_space_id: int | None, concept_id: int | None) -> CanonProjectionResult`:
  build an async engine + sessionmaker, build a `Neo4jClient.from_env()`, call `project_canon`,
  close both. Resolve a default `search_space_id = MIN(aita_search_spaces.id)` when neither scope
  arg is given (matching the WU-3B seeder's bootstrap convenience) — but still pass an explicit
  scope into `load_entity_specs` so the unscoped-refusal stays intact.
- `def main(argv) -> int`: argparse `--database-url` (default env `DATABASE_URL`), `--search-space-id`,
  `--concept-id`, `--verbose`. Prints the result stats. Reads Neo4j creds from env (never hardcoded;
  same `Neo4jClient.from_env()` contract).
- Operational note in docstring: this is the rebuild primitive — "`:Canon` is rebuildable from
  Postgres at any time" (§2). Safe to re-run; idempotent.
- Verify: `python -m scripts.seed_canon_projection --help` exits 0; CLI smoke test
  (`test_seed_canon_projection_cli.py`) asserts arg parsing + that `run` calls `project_canon` with
  the scoped args (mock `project_canon` + `Neo4jClient`, no live infra).

### 6. Layer-2 node scoping + graded_at helper (store.py)

- [ ] File: `apollo/knowledge_graph/store.py` (edit)
- **`write_nodes` scoping kwargs (backward-compatible).** Extend the signature to:
  `async def write_nodes(self, *, attempt_id, nodes, source, user_id: str | None = None,
  search_space_id: int | None = None) -> int`. The two new kwargs default to `None` so every
  existing caller (`chat.py`, tests) keeps working unchanged. When provided, they are injected into
  each created node's property bag plus a `created_at` ISO-8601 UTC timestamp. Implementation:
  inside the `rows_by_label` build loop, after `row = _node_to_neo4j_props(n)`, add
  `if user_id is not None: row["user_id"] = user_id`, `if search_space_id is not None:
  row["search_space_id"] = search_space_id`, and always `row["created_at"] = <utc iso8601 now>`.
  Follow the existing `None`-omission convention (store.py:103) — omit `user_id`/`search_space_id`
  when None rather than writing a literal "None".
- **`_record_to_node` tolerance (read path).** The new props (`user_id`, `search_space_id`,
  `created_at`, `graded_at`) must NOT break `_record_to_node`: it currently `bag.pop`s the known
  keys and passes the rest into `build_node` as content. Add `bag.pop("user_id", None)`,
  `bag.pop("search_space_id", None)`, `bag.pop("created_at", None)`, `bag.pop("graded_at", None)`
  so these scoping/timestamp props are stripped before content reconstruction (they are NOT node
  content — they are metadata). This keeps `read_graph` round-tripping correctly for scoped nodes.
- **`stamp_graded_at` helper (NEW method).** `async def stamp_graded_at(self, *, attempt_id: int) -> int`:
  one scoped Cypher `MATCH (n:_KGNode {attempt_id: $aid}) SET n.graded_at = $ts RETURN count(n) AS stamped`
  with `ts = <utc iso8601 now>`. Returns the count stamped. Wrap in try/except →
  `RetentionError(attempt_id=attempt_id, last_error=str(exc))`. Idempotent (re-stamp overwrites the
  same field; Δt anchoring in Layer-3 reads the stored value, never `now()`, §3). Does NOT touch
  Postgres.
- **No change to `write_edges`** — edges are not Layer-2 scoping targets in this unit.
- Verify: `test_store_scoping.py` (fake-session unit) + `test_store_graded_at_neo4j.py` (integration)
  green; existing `test_store_*` suites still pass (backward-compat).

### 7. Retention change (lifecycle.py / done.py / restart_problem.py)

- [ ] **File: `apollo/handlers/lifecycle.py` (edit) — `handle_end` STOPS deleting.**
  Remove the per-attempt `delete_subgraph` loop (lines 39-52) entirely. `handle_end` now only marks
  the session `ended` in Postgres (the `sess.status = SessionStatus.ended.value; await db.commit()`
  block stays) and returns `{"ok": True}`. Per-attempt Neo4j subgraphs PERSIST — cross-attempt
  connectivity comes free (two attempts resolving to the same `:Canon` node are two hops apart, §7).
  Update the docstring: "Marks the session ended. Per-attempt Neo4j subgraphs are PERSISTED (§7
  retention change, WU-3C1) — a future janitor prunes old subgraphs; `restart_problem` is the only
  explicit student wipe." The unused `KGStore`/`Neo4jClient`/`ProblemAttempt` imports that only
  served the delete loop: keep `Neo4jClient` (still in `handle_get_session` signature) but the
  `attempts` query + `store` construction in `handle_end` are removed. Verify no other code in
  `handle_end` references them.
- [ ] **File: `apollo/handlers/done.py` (edit) — stamp `graded_at`.**
  Call `await store.stamp_graded_at(attempt_id=attempt.id)` as the LAST step before `return`, AFTER
  `apply_xp` and `compute_progress_envelope` have run and after the grade commit (line 278) — i.e.
  immediately before building the return dict (after line 290, before the `return {` at line 292).
  Placement matters (Path A, see Error contract): the student-facing grade + XP are already committed
  and the envelope computed, so a `RetentionError` from the stamp surfaces (as a 500, NO-FALLBACK)
  WITHOUT voiding the grade — the next Done/retry/janitor re-stamps idempotently. Reuse the existing
  `store = KGStore(db, neo)` (line 196). Update the `handle_done` docstring to record that the
  `graded_at` stamp is the final, idempotent, post-commit retention write (§7 / §6.4).
- [ ] **File: `apollo/handlers/restart_problem.py` (edit — ASSERT behavior preserved, minimal change).**
  `restart_problem` KEEPS deleting: the `await store.delete_subgraph(attempt_id=current_attempt.id)`
  at line 65 STAYS. This is the explicit student wipe. The only change is a clarifying comment:
  "Retention (§7): restart_problem is the ONE explicit student wipe that still deletes the subgraph
  — handle_end now persists (WU-3C1)." No behavioral change; a regression test pins that
  `delete_subgraph` is still called.
- [ ] **`delete_subgraph` stays in `KGStore`** unchanged — it is the future janitor's primitive
  (§7) and `restart_problem`'s wipe. Do not remove it.
- Verify: `apollo/knowledge_graph/tests/test_retention_handlers.py` green (tests 26-29).

### 8. Neo4j schema file (neo4j_schema.cypher)

- [ ] File: `apollo/persistence/neo4j_schema.cypher` (edit — append, never reorder existing)
- Append a `:Canon` uniqueness constraint:
  `CREATE CONSTRAINT canon_key_unique IF NOT EXISTS FOR (c:Canon) REQUIRE c.key IS UNIQUE;`
  This is what makes `MERGE (c:Canon {key: ...})` an upsert (not a duplicate-create) and enforces the
  one-node-per-entity-id invariant at the DB level.
- Append a `:_KGNode(user_id)` index:
  `CREATE INDEX kgnode_user_id IF NOT EXISTS FOR (n:_KGNode) ON (n.user_id);`
  The existing `kgnode_attempt_id` index (line 25) STAYS (binding: keep the attempt_id index).
- Optional `:Canon(search_space_id)` index for course-scoped Cypher filters (WU-3C2 will filter
  `:Canon` by `search_space_id`): `CREATE INDEX canon_search_space_id IF NOT EXISTS FOR (c:Canon) ON (c.search_space_id);`
- **Test harness note:** the Testcontainers `neo4j_client` fixture does NOT run this `.cypher` file
  automatically (it only wipes `MATCH (n) DETACH DELETE n`). The `:Canon` MERGE works WITHOUT the
  uniqueness constraint (MERGE matches on the property pattern regardless), so the idempotency test
  passes on a constraint-free container. To ALSO assert the constraint behaves, the integration test
  applies `canon_key_unique` to the container at setup (run the constraint Cypher once before the
  MERGE) — see the fusion-isolation test. The constraint file is the prod/Aura authority.
- Verify: `test_neo4j_schema_canon.py` reads the `.cypher` text and asserts the three new statements
  are present + idempotent (`IF NOT EXISTS`).

### 9. Tests

See the **Full test list** section for every test name, assertion, mocking strategy, and which
container fixtures it needs. Test files (all REAL tests, no skip/xfail, no assert-nothing):

- Fast pure-unit (no Docker): `apollo/knowledge_graph/tests/test_canon_projection_mapping.py`,
  `apollo/knowledge_graph/tests/test_store_scoping.py` (fake async session),
  `apollo/knowledge_graph/tests/test_neo4j_schema_canon.py` (text assertions),
  `scripts/` CLI test `tests/database/test_seed_canon_projection_cli.py` (mock infra).
- Real-infra Neo4j (`neo4j_client` fixture): `apollo/knowledge_graph/tests/test_canon_projection_seeder_neo4j.py`,
  `apollo/knowledge_graph/tests/test_store_graded_at_neo4j.py`.
- Real-infra Postgres + Neo4j (`db_session` + `neo4j_client`):
  `tests/database/test_canon_projection.py` (the load-bearing fusion-isolation test seeds two
  concepts in Postgres, projects, asserts two distinct `:Canon` nodes).
- Handler retention tests (fake/mock session, no live infra needed for behavior assertions). **Scope
  note:** the WU-3C1 scope file list allows `apollo/knowledge_graph/tests/**` and `tests/database/**`
  but NOT a new `apollo/handlers/tests/**` dir, and the handler change is a retention/store-contract
  change — so these tests live under `apollo/knowledge_graph/tests/` (they assert store-interaction
  behavior of the handlers): `apollo/knowledge_graph/tests/test_retention_handlers.py` holds
  `test_handle_end_does_not_delete_subgraph`, `test_done_stamps_graded_at`,
  `test_restart_problem_still_deletes_subgraph`. (`apollo/handlers/lifecycle.py|done.py|restart_problem.py`
  are imported and called with mocked sessions.)

**BOTH real-infra gates fire** (Postgres `tests/database` + Neo4j `apollo/knowledge_graph/tests`).
Docker is up; these MUST run green, not skip. All test attempt_ids are NEGATIVE (cleanup deletes
`attempt_id < 0`).

## Public signatures

```python
# apollo/errors.py  (append)
class CanonProjectionError(ApolloError):
    def __init__(self, *, stage: str, last_error: str) -> None: ...
        # message: f"Canon projection failed at stage {stage!r}: {last_error}"

class RetentionError(ApolloError):
    def __init__(self, *, attempt_id: int, last_error: str) -> None: ...
        # message: f"Retention operation failed for attempt {attempt_id}: {last_error}"


# apollo/knowledge_graph/canon_projection.py  (NEW)
@dataclass(frozen=True)
class CanonNodeSpec:
    key: int                # == apollo_kg_entities.id (surrogate)
    search_space_id: int
    concept_id: int
    canonical_key: str
    kind: str
    display_name: str

@dataclass(frozen=True)
class CanonProjectionResult:
    merged: int
    entity_count: int

def entity_to_canon_spec(
    entity_id: int, *, concept_id: int, search_space_id: int,
    canonical_key: str, kind: str, display_name: str,
) -> CanonNodeSpec: ...

def canon_spec_to_row(spec: CanonNodeSpec) -> dict[str, Any]: ...

async def load_entity_specs(
    db: AsyncSession, *,
    search_space_id: int | None = None, concept_id: int | None = None,
) -> list[CanonNodeSpec]: ...   # raises ValueError if neither scope given; CanonProjectionError on DB failure

async def project_canon(
    db: AsyncSession, neo: Neo4jClient, *,
    search_space_id: int | None = None, concept_id: int | None = None,
) -> CanonProjectionResult: ...  # raises CanonProjectionError on Neo4j failure


# apollo/knowledge_graph/store.py  (edit — KGStore)
async def write_nodes(
    self, *, attempt_id: int, nodes: list[Node], source: str,
    user_id: str | None = None, search_space_id: int | None = None,  # NEW, default None = back-compat
) -> int: ...

async def stamp_graded_at(self, *, attempt_id: int) -> int: ...  # NEW; raises RetentionError on failure


# scripts/seed_canon_projection.py  (NEW)
async def run(
    database_url: str, *, search_space_id: int | None = None, concept_id: int | None = None,
) -> CanonProjectionResult: ...

def main(argv: list[str] | None = None) -> int: ...
```

**Backward-compat guarantees:**
- `write_nodes` new kwargs are keyword-only and default `None` — zero existing caller changes.
- `handle_end` keeps its `(*, db, neo, session_id)` signature (the `neo` param stays even though the
  delete loop is gone — `api.py` passes it positionally-by-keyword and `handle_get_session` shares
  the wiring). No caller signature change.
- `delete_subgraph` and `read_graph` are untouched.

## DI / dependency discovery findings

This codebase uses **explicit constructor/parameter injection**, not a DI container. Findings from
three sibling call sites:

1. **`Neo4jClient` provenance.** Constructed once per process and threaded explicitly. `apollo/api.py`
   holds the process singleton (`get_neo4j_client`/`close_neo4j_client`, apollo.md:29). Handlers
   receive it as a `neo: Neo4jClient` keyword arg (`handle_end`, `handle_done`,
   `handle_restart_problem` all take `*, db, neo, session_id`). `Neo4jClient.from_env()` reads creds
   from env only (neo4j_client.py:27). → The `:Canon` seeder takes `neo: Neo4jClient` as a parameter
   (same as `KGStore.__init__`); the CLI builds it via `from_env()`. Singleton, not request-scoped.
2. **`AsyncSession` provenance.** Repo-wide async SQLAlchemy session from
   `database.session.get_db_session` (apollo.md:109), injected into handlers and `KGStore(db, neo)`
   as a constructor arg. The seeder takes `db: AsyncSession`. In tests it is the `db_session`
   fixture (pgvector, savepoint-rollback) or a fresh `async_sessionmaker` (the WU-3B seeder pattern
   for commit-observing tests). → `project_canon(db, neo, ...)` mirrors `KGStore(db, neo)`.
3. **Entity-row read pattern.** `scripts/seed_apollo_learner_model.py:149` shows the exact join to
   recover course scope: `select(...).join(Subject, Subject.id == Concept.subject_id)
   .where(Subject.search_space_id == ...)`. `load_entity_specs` reuses this join shape
   (`KGEntity → Concept → Subject`) to carry `search_space_id` onto each `CanonNodeSpec`.

No injection-token ambiguity (no container, no string tokens). Everything is a plain typed parameter.
No sibling-convention disagreement.

## Transaction scope decisions

This unit's writes span TWO stores but **never need a cross-store transaction** — each store's write
is independently idempotent, matching the §6.4 "there is no cross-store transaction" stance.

- **:Canon projection (Postgres read → Neo4j write).** Read-only on Postgres; the Neo4j side is a
  single `MERGE` batch in one Neo4j session. No transaction spanning the two. If the Neo4j MERGE
  partially fails, the seeder raises `CanonProjectionError` and the projection is simply re-run
  (idempotent MERGE → safe). No Postgres state changes, so nothing to roll back. The projection is
  "rebuildable from Postgres at any time" (§2) precisely because it holds no transaction.
- **Layer-2 `write_nodes` scoping.** Unchanged transaction shape: the existing single Neo4j session
  per call (store.py:267). Adding props to the property bag does not change atomicity.
- **`stamp_graded_at` at Done.** Runs AFTER the Postgres grade transaction commits (done.py:278), in
  its own Neo4j session. Single statement, idempotent (overwrites the same field). This is the §6.4
  ordering: grade commits first (student-facing, durable), then the best-effort/idempotent Neo4j
  metadata write. A `stamp_graded_at` failure raises `RetentionError` but cannot void the grade.
- **`handle_end` retention.** Now a single Postgres commit (`status = ended`) and ZERO Neo4j writes —
  strictly simpler than before (the delete loop is gone). No transaction concern.

**No external service (OpenAI/Whisper/network) is touched by this unit** — so there is no
compensation-strategy obligation. The §5 resolver's LLM adjudication (which would have one) is
WU-3C2, out of scope here.

## Error contract decisions

Existing convention (apollo.md:115): every failure is a named `ApolloError` subclass in
`apollo/errors.py`; HTTP handlers are registered in `api.py` via `register_exception_handlers`;
NO-FALLBACK (never soft-fail a grade or silently truncate). WU-3C1 follows it exactly.

- **`CanonProjectionError`** — raised by `load_entity_specs` (stage `load_entities`) and
  `project_canon` (stage `merge_canon`). Surfaced through the CLI as a non-zero exit + logged
  traceback (it is an operational script, not a request). **No `api.py` handler** is added because
  no HTTP route triggers the projection in WU-3C1 (the auto-rebuild-on-promotion trigger is §8B,
  future). Adding an `api.py` handler would breach the WU-3C1 scope file list. When WU-3C2/§8B wires
  a route that rebuilds `:Canon`, THAT unit registers the handler. Documented here so it is a
  deliberate choice, not an omission.
- **`RetentionError`** — raised by `stamp_graded_at`. Inside `handle_done` it propagates AFTER the
  grade/XP have committed (done.py:278-284), so the student's earned grade is never voided (§5
  NO-FALLBACK). **Precision (verified in `api.py:402`):** there is NO generic `ApolloError` fallback
  handler — each error class is registered explicitly in `register_exception_handlers`. So an
  UN-registered `RetentionError` would surface as a raw 500. WU-3C1 must decide ONE of two paths and
  the executor picks based on whether `api.py` editing is acceptable:
  - **Path A (in-scope, preferred):** do NOT register a handler (api.py is out of WU-3C1 scope).
    `stamp_graded_at`'s `RetentionError` is allowed to surface as a generic 500 because the grade is
    already durable and the next Done/retry/janitor re-stamps idempotently. The plan's binding shape:
    the stamp is the LAST thing `handle_done` does, after the response envelope's data is already
    committed — so even a raw 500 leaves a correct, graded, XP-awarded attempt; only the (idempotent)
    `graded_at` stamp is pending. Document this explicitly in the `handle_done` docstring.
  - **Path B (only if api.py is later added to scope):** register a `RetentionError` handler returning
    a 503-style "retention pending, retry" envelope matching `CoverageGradingError`'s shape. NOT done
    in WU-3C1 (would breach scope).
  WU-3C1 takes **Path A**. The grade is durable; the stamp is best-effort-but-named (so failures are
  visible in logs/500, never silently swallowed) and idempotently retried.
- **Final response shape to the client:** unchanged on the success path for all endpoints.
  `handle_end` still returns `{"ok": True}`; `handle_done` still returns the
  rubric/diagnostic/progress envelope (the stamp runs AFTER the envelope data is committed). The only
  new client-visible behavior is the rare case where the post-grade `graded_at` stamp fails: under
  Path A this surfaces as a generic 500 (no registered handler — api.py is out of scope), but the
  grade/XP are ALREADY durable and the stamp is idempotently retried, so no data is lost and nothing
  is silently downgraded (NO-FALLBACK satisfied: the failure is loud, not swallowed). No new
  exception hierarchy is introduced (both new classes subclass the existing `ApolloError`).

## Full test list

Each test: name · what it asserts · how external deps are mocked. Patch-coverage target ≥ 95% on
changed lines (diff-cover vs `feat/apollo-kg-wu3b-bernoulli-seed`). Every branch of the new code
(scope-selector, None-omission, error wrapping, idempotency) is covered.

### A. Pure-unit — no Docker, no network — `apollo/knowledge_graph/tests/test_canon_projection_mapping.py`

1. `test_entity_to_canon_spec_carries_all_properties` — `entity_to_canon_spec(42, concept_id=7,
   search_space_id=3, canonical_key="eq.continuity", kind="equation", display_name="Continuity")`
   returns a `CanonNodeSpec` with `key == 42` and every field carried verbatim. No mocks (pure fn).
2. `test_canon_spec_key_is_surrogate_entity_id` — asserts `spec.key` equals the entity id passed,
   NOT a synthesized `f"{search_space_id}:{canonical_key}"`. Pins the load-bearing decision.
3. `test_canon_spec_to_row_shape` — `canon_spec_to_row(spec)` returns exactly the 6-key dict
   `{key, search_space_id, concept_id, canonical_key, kind, display_name}` (no extra keys, no
   `RESOLVES_TO`/resolution fields). Pure.
4. `test_misconception_kind_maps_unchanged` — a spec with `kind="misconception"`,
   `canonical_key="misc.density_ignored"` maps to a row with `kind == "misconception"` (misconception
   entities ARE projected; no kind filtering in the mapper). Pure.
5. `test_canon_node_spec_is_frozen` — `dataclasses.replace` works but attribute assignment raises
   `FrozenInstanceError` (immutability rule). Pure.
6. `test_merge_cypher_uses_key_only_and_merge` — assert `_CANON_MERGE_CYPHER` contains
   `MERGE (c:Canon {key: row.key})` and `count(c) AS merged`, and does NOT contain `CREATE (c:Canon`
   (guards against an accidental CREATE that would duplicate on re-run). Text assertion, pure.

### B. Real-infra Neo4j — `neo4j_client` fixture — `apollo/knowledge_graph/tests/test_canon_projection_seeder_neo4j.py`

Uses `neo4j_client` only (entities passed as in-memory `CanonNodeSpec` lists via a thin
`_merge_specs(neo, specs)` seam, OR by calling `project_canon` with a stubbed `db` that returns the
specs). Prefer testing the Neo4j-write half directly against the container with hand-built specs so
the Postgres source is mocked deterministically (a `_FakeDb` whose `load_entity_specs` is
monkeypatched to return fixed specs), keeping this file Neo4j-only. NEGATIVE-keyed not applicable to
`:Canon` (no attempt_id) — instead the per-test wipe (`MATCH (n) DETACH DELETE n`) isolates.

7. `test_project_merges_one_canon_node_per_entity` — project 3 specs (1 equation, 1 condition, 1
   misconception); query `MATCH (c:Canon) RETURN count(c)` == 3; each carries its `key`,
   `canonical_key`, `kind`, `display_name`, `search_space_id`, `concept_id`. Postgres mocked via
   stub returning the 3 specs.
8. `test_project_is_idempotent_same_count_second_run` — run `project_canon` TWICE over the same
   specs; `MATCH (c:Canon) RETURN count(c)` is the SAME after both runs (no duplicates); the
   `CanonProjectionResult.merged` reflects the MERGE. THE idempotency guarantee.
9. `test_misconception_entity_projected_as_canon` — a `kind="misconception"`, key `misc.*` spec
   produces a `:Canon` node with `kind == "misconception"` readable in Neo4j.
10. `test_project_neo4j_failure_raises_canon_projection_error` — inject a `Neo4jClient` whose
    `session()` raises on `run` (a failing fake); assert `project_canon` raises
    `CanonProjectionError(stage="merge_canon")`, message contains the underlying error. NO-FALLBACK.
11. `test_key_property_is_int_equal_to_entity_id` — after projection, `c.key` in Neo4j equals the
    integer entity id (type + value), proving the surrogate-id keying survived the round trip.

### C. Real-infra Postgres + Neo4j — `db_session` + `neo4j_client` — `tests/database/test_canon_projection.py`

The load-bearing isolation file. Reuses the WU-3B `tests/database/test_seed_apollo_learner_model.py`
DB-build helpers (fresh migrated DB, `_seed_bernoulli_curriculum`) OR seeds entities directly via
`KGEntity` ORM inserts on `db_session` for speed. Marked `@pytest.mark.integration`.

12. `test_load_entity_specs_scoped_by_concept` — seed entities under 2 concepts; `load_entity_specs(
    db, concept_id=C1)` returns only C1's entities, each carrying the correct `search_space_id` from
    the `Subject` join. Real Postgres.
13. `test_load_entity_specs_scoped_by_search_space` — seed 2 courses; `load_entity_specs(db,
    search_space_id=S1)` returns only S1's entities across all its concepts. Real Postgres.
14. `test_load_entity_specs_refuses_unscoped` — `load_entity_specs(db)` with neither scope raises
    `ValueError` (course-blind projection forbidden, isolation invariant §1.4). Real Postgres.
15. `test_load_entity_specs_includes_misconceptions` — seed a `kind="misconception"` entity; it
    appears in the returned specs (no kind filter). Real Postgres.
16. **`test_two_concepts_same_canonical_key_project_to_distinct_canon_nodes`** — THE fusion-isolation
    test. Seed concept A and concept B (could be two different fluid concepts, even in different
    courses) that EACH mint an entity with `canonical_key="eq.continuity"` — two distinct
    `apollo_kg_entities` rows with distinct surrogate ids (allowed: uniqueness is per-concept). Run
    `project_canon` for both. Assert `MATCH (c:Canon {canonical_key:"eq.continuity"}) RETURN count(c)`
    == 2 (TWO distinct nodes, distinct `key` values == the two entity ids) — proving the surrogate-id
    key prevents fusion and cross-contamination. This is the central guarantee of the unit. Real
    Postgres + real Neo4j.
17. `test_load_then_project_carries_db_properties_end_to_end` — seed a real entity row, run
    `project_canon`, read the `:Canon` node back from Neo4j, assert `display_name`/`kind`/
    `canonical_key`/`concept_id`/`search_space_id` match the Postgres row exactly. End-to-end.
18. `test_canon_key_unique_constraint_blocks_duplicate_key` — apply `canon_key_unique` to the
    container, then attempt two CREATEs with the same `key`; the second errors (constraint works).
    Proves the schema constraint is real (defense-in-depth behind MERGE). Real Neo4j.

### D. Layer-2 scoping — fake async session — `apollo/knowledge_graph/tests/test_store_scoping.py`

Reuses the existing fake-session pattern from `test_store_write_edges.py` / `test_store_negotiation.py`
(no live driver — a fake async Neo4j session captures the `$rows` passed to CREATE).

19. `test_write_nodes_injects_user_id_and_search_space_id` — call `write_nodes(..., user_id="u-1",
    search_space_id=5)`; the captured create row carries `user_id == "u-1"`, `search_space_id == 5`,
    and a `created_at` string. Fake session.
20. `test_write_nodes_omits_none_scoping_props` — call `write_nodes(...)` WITHOUT the new kwargs; the
    captured row has NO `user_id`/`search_space_id` keys (None-omission convention) but still gets
    `created_at`. Back-compat. Fake session.
21. `test_write_nodes_backcompat_return_int` — existing callers' return contract (count of created
    nodes) is unchanged when new kwargs absent. Fake session.
22. `test_record_to_node_strips_scoping_props` — feed `_record_to_node` a props bag containing
    `user_id`/`search_space_id`/`created_at`/`graded_at`; reconstruction succeeds and those keys do
    NOT leak into node content (they are popped). Pure (no session).

### E. graded_at helper — real Neo4j — `apollo/knowledge_graph/tests/test_store_graded_at_neo4j.py`

NEGATIVE attempt_ids (cleanup deletes `attempt_id < 0`). `KGStore` built with a `_Stub` Postgres
session (the `test_store_neo4j.py:77` pattern) since `stamp_graded_at` never touches Postgres.

23. `test_stamp_graded_at_sets_timestamp_on_all_nodes` — write 2 nodes at `attempt_id=-501`, call
    `stamp_graded_at(attempt_id=-501)`; query the nodes, both carry a non-null `graded_at`; return
    count == 2. Real Neo4j.
24. `test_stamp_graded_at_idempotent` — call twice; node count and `graded_at` presence unchanged
    (overwrite, not duplicate); no error. Real Neo4j.
25. `test_stamp_graded_at_failure_raises_retention_error` — inject a failing fake Neo4j session;
    assert `RetentionError(attempt_id=-501)` raised, NO-FALLBACK. Fake session (can live in the
    scoping file or here).

### F. Retention handlers — `apollo/knowledge_graph/tests/test_retention_handlers.py`

26. `test_handle_end_does_not_delete_subgraph` — patch `KGStore.delete_subgraph` as a spy (AsyncMock);
    call `handle_end` with a mocked Postgres session (returns an `ApolloSession`) and a mock
    `Neo4jClient`; assert `delete_subgraph` is NEVER called; assert the session status is set to
    `ended` and `db.commit()` awaited. No live infra. (Mock pattern: a lightweight async session stub
    whose `execute(...).scalar_one()` returns a settable session object, mirroring the
    `test_store_neo4j.py:77` `_Stub` idea.)
27. `test_handle_end_subgraph_persists_real_neo4j` (integration) — OPTIONAL stronger variant: write
    nodes at `attempt_id=-601` via the real `neo4j_client`, run the `handle_end` Postgres path with a
    stub session, then `read_graph(-601)` STILL returns the nodes (persistence proven against a real
    container). Real Neo4j + stub Postgres. Lives in the same file.
28. `test_done_stamps_graded_at` — patch `KGStore.stamp_graded_at` as an AsyncMock spy and the
    grading internals (`compute_coverage`/`compute_rubric`/`generate_diagnostic`/`apply_xp`/
    `_find_problem`) so no LLM/Neo4j fires; assert `stamp_graded_at` is called once with
    `attempt_id=attempt.id` AFTER the grade commit (assert call ordering via a recording mock).
    Deterministic mocks, no network.
29. `test_restart_problem_still_deletes_subgraph` — call `handle_restart_problem` with a
    `delete_subgraph` spy and a mocked session in an allowed phase; assert it IS called once with the
    current attempt id (regression pin that restart keeps wiping). No live infra.

### G. CLI — `tests/database/test_seed_canon_projection_cli.py`

30. `test_main_parses_scope_args_and_calls_run` — patch `scripts.seed_canon_projection.run` (or
    `project_canon` + `Neo4jClient.from_env`) as AsyncMock; invoke `main(["--database-url", "...",
    "--concept-id", "7"])`; assert `run` called with `concept_id=7`; exit code 0. No live infra.
31. `test_main_missing_db_url_returns_error` — `main([])` with no `DATABASE_URL` env returns the
    documented non-zero code (mirrors the WU-3B CLI). No infra.
32. `test_run_builds_neo4j_from_env_and_projects` — patch `Neo4jClient.from_env` + `project_canon`;
    assert `run(...)` calls `project_canon` once with the scoped args and returns its result. Mocked.

### H. Schema text — `apollo/knowledge_graph/tests/test_neo4j_schema_canon.py`

33. `test_schema_declares_canon_key_constraint` — read `neo4j_schema.cypher`; assert it contains
    `CREATE CONSTRAINT canon_key_unique IF NOT EXISTS FOR (c:Canon) REQUIRE c.key IS UNIQUE`. Pure.
34. `test_schema_declares_user_id_index` — assert `CREATE INDEX kgnode_user_id IF NOT EXISTS FOR
    (n:_KGNode) ON (n.user_id)` present; assert the existing `kgnode_attempt_id` index line STILL
    present (attempt_id index preserved). Pure.
35. `test_schema_statements_idempotent` — every new statement uses `IF NOT EXISTS`. Pure.

**Coverage note:** the only lines plausibly hard to hit are the `CanonProjectionError`/`RetentionError`
except branches — covered by tests 10 and 25 (injected failing fakes). No `# pragma: no cover` is
needed; no line is exempted.

## Owner-doc updates

Owner doc: `docs/architecture/apollo.md` (`owns: apollo/**`). Drift contract: update in the SAME work
and set `last_verified: 2026-06-16`.

- [ ] **Frontmatter:** bump `last_verified` from `2026-06-15` to `2026-06-16`.
- [ ] **Module map — `apollo/knowledge_graph/` row:** add `canon_projection.py` — "the `:Canon`
  projection seeder: reads Layer-1 `apollo_kg_entities` from Postgres and idempotently `MERGE`s
  `:Canon` Neo4j nodes keyed on `key = entity.id` (surrogate id prevents same-`canonical_key`
  fusion across concepts); carries `search_space_id/concept_id/canonical_key/kind/display_name`;
  misconception entities included; rebuildable, never authored in Neo4j (WU-3C1)." Note `store.py`
  gains `stamp_graded_at` + `write_nodes` scoping kwargs (`user_id`/`search_space_id`/`created_at`).
- [ ] **Module map — `apollo/persistence/` row:** note `neo4j_schema.cypher` gains the `:Canon(key)`
  uniqueness constraint + `:_KGNode(user_id)` + `:Canon(search_space_id)` indexes.
- [ ] **Module map — `scripts` reference (in domain-data.md cross-link / apollo.md):** add
  `scripts/seed_canon_projection.py` as the `:Canon` rebuild CLI.
- [ ] **HTTP routes table — `POST /apollo/sessions/{id}/end` row:** change "best-effort deletes every
  per-attempt subgraph" → "marks ended; per-attempt Neo4j subgraphs PERSIST (§7 retention,
  WU-3C1) — `restart_problem` is the only explicit wipe." Update the
  `POST /apollo/sessions/{id}/done` row to note it stamps `graded_at` on the frozen graph.
- [ ] **Non-obvious conventions — Freeze/retention bullet (apollo.md:122):** replace "session end is
  best-effort with logged failures" with the new retention rule: "`handle_end` PERSISTS subgraphs
  (no delete); `done.py` stamps `graded_at`; `restart_problem` is the explicit student wipe;
  `delete_subgraph` remains the future janitor's primitive; `attempt_id < 0` test-cleanup unchanged
  (§7)."
- [ ] **Non-obvious conventions — Neo4j shape bullet (apollo.md:119):** add the `:Canon` label and the
  Layer-2 scoping props (`user_id`/`search_space_id`/`created_at`/`graded_at`) to the documented
  node-shape, and note the `key`-surrogate-id fusion-avoidance rationale.
- [ ] **NO-FALLBACK bullet:** add `CanonProjectionError` and `RetentionError` to the named-error
  inventory; note they are NOT registered as HTTP handlers in this unit (operational/post-grade
  surfaces) and why.
- Verify: `grep -n "last_verified: 2026-06-16" docs/architecture/apollo.md` and `grep -n "Canon" docs/architecture/apollo.md` both non-empty.

## Downstream consumers

- **WU-3C2 (the NEXT unit)** is the primary consumer: its resolver writes
  `(:_KGNode)-[:RESOLVES_TO]->(:Canon)` edges against the `:Canon` nodes this unit projects, and
  reads the Layer-2 `user_id`/`search_space_id`/`graded_at` props this unit writes. WU-3C1 lands the
  targets; WU-3C2 lands the edges + resolution fields.
- **Layer-3 (§3) belief update** reads `graded_at` for Δt-anchoring (decay anchored to the freeze/Done
  timestamp, never `now()`) — this unit makes `graded_at` exist on the graph.
- **§8B auto-provisioning** will call `project_canon` (or the CLI) on `:Canon` rebuild after problem
  promotion. WU-3C1 provides the rebuildable primitive.
- **Frontend:** NO frontend consumes `:Canon` or the new Neo4j props directly (they are internal
  graph state). The `GET /apollo/sessions/{id}` KG payload (`graph.model_dump`) is unaffected because
  `_record_to_node` STRIPS the scoping/timestamp props before reconstruction (test 22), so the
  FE-visible KG shape is byte-identical. Grep confirmed: no UI repo references `:Canon`, `graded_at`,
  or `user_id` Neo4j props (these are new). The retention change is FE-invisible: `handle_end` still
  returns `{"ok": True}`.

## Risks

| Risk | Confidence it's handled | Mitigation |
|---|---|---|
| **Fusion of same-`canonical_key` entities across concepts** — the entire reason for surrogate-id keying. | HIGH | `key = entity.id` (never `search_space_id:canonical_key`); test 16 proves two distinct `:Canon` nodes for two concepts both minting `eq.continuity`. |
| **`write_nodes` signature change breaks existing callers.** | HIGH | New kwargs keyword-only + default `None`; tests 20/21 pin back-compat; existing `test_store_*` suites must stay green. |
| **`_record_to_node` chokes on new props** (would corrupt `read_graph`). | HIGH | Test 22 pins that scoping/timestamp props are popped before content reconstruction; the GET-session KG shape is unchanged. |
| **Testcontainers `neo4j_client` does not load `neo4j_schema.cypher`**, so MERGE runs without the uniqueness constraint. | MEDIUM | MERGE matches on the property pattern regardless of constraint, so idempotency (test 8) holds constraint-free; test 18 applies the constraint explicitly to prove it works. The `.cypher` file is the prod/Aura authority (run once on the instance). |
| **`handle_end` import cleanup** — removing the delete loop leaves possibly-unused imports (`KGStore`, `ProblemAttempt`). | MEDIUM | Keep `Neo4jClient` (used by `handle_get_session`); remove only the now-dead `attempts` query + `store` construction in `handle_end`; run ruff to catch unused imports; `handle_get_session` still imports `KGStore`/`list_problems_for_cluster`, so the module-level import of `KGStore` stays. |
| **Aura free-tier capacity** now that subgraphs persist. | LOW (informational) | §7 math: ~90k nodes/170k rels per year, within 200k/400k limits (~2yr headroom). Janitor pruning is future work, out of scope. Recorded, not actioned. |
| **`stamp_graded_at` failure voids a grade.** | HIGH | Stamp runs AFTER the grade commits (done.py:278); `RetentionError` surfaces post-commit, grade/XP already durable (§6.4). |
| **Two real-infra gates both must be green (not skip).** | HIGH | Docker is up; tests use the conftest `db_session` + `neo4j_client` fixtures; CI runs `pytest tests/database` AND `pytest apollo/knowledge_graph/tests`. No skip/xfail anywhere. |
| **Patch coverage < 95%.** | MEDIUM | Error branches covered by injected-failure tests (10, 25); scope-selector branches by tests 12-14; None-omission by 19-20. Run `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b-bernoulli-seed --fail-under=95` before done. |

## Out-of-scope boundaries (WU-3C1)

Explicitly NOT built here (these are WU-3C2 / later units — building them now breaches the unit):

- **The §5 RESOLVER** — tier matching (exact → SymPy structural equivalence → alias/normalization →
  RapidFuzz), misconception competition, global/greedy assignment, the one LLM adjudication call.
- **`RESOLVES_TO` edges** `(:_KGNode)-[:RESOLVES_TO {method, confidence, resolved_at}]->(:Canon)`.
- **Resolution node-fields** on Layer-2 nodes: `resolution` (resolved/unresolved/ambiguous),
  `resolved_key`, `resolution_method`, `resolution_confidence`. (We add scoping/timestamp props ONLY.)
- **Reading `:Canon`** for matching — projection is write-only in this unit.
- **The §6 grading core** (`apollo/graph_compare/**`: validator, canonical_graph, simulation,
  bisimilarity, transcript_audit, findings, events) and **Layer-3 belief math**.
- **§8A runtime cutover** (DB-driven concept resolution, deleting `_AVAILABLE_CLUSTERS` /
  `_CLUSTER_TO_CONCEPT`) and **§8B auto-provisioning pipeline.**
- **Any Postgres migration** — no new numbered SQL; no remote DB touched (test/prod), Testcontainers
  only.
- **`api.py` HTTP handler registration** for the new errors — deferred to the unit that wires a route
  triggering projection/rebuild. `api.py` is outside the WU-3C1 scope file list.
- **The janitor** that prunes old subgraphs — `delete_subgraph` is left as its primitive only.

## Deviations I'd allow the executor

- **Where the Neo4j-write half is tested from in file B** — calling `project_canon` with a stubbed
  `db` vs. a small `_merge_specs(neo, specs)` seam extracted from `project_canon`. Either is fine as
  long as the Postgres source is deterministically mocked and the Neo4j MERGE runs against the real
  container. If extracting `_merge_specs` makes the failure-injection test (10) cleaner, do it.
- **Seeding entities in file C** — via the full WU-3B `_seed_bernoulli_curriculum` DB-build harness
  OR via direct `KGEntity` ORM inserts on `db_session`. Prefer the lighter ORM-insert path for the
  fusion test (test 16) since it only needs two entities under two concepts; use the heavier harness
  only if a real bernoulli fixture is needed.
- **`created_at`/`graded_at` timestamp format** — ISO-8601 UTC string is the assumption (matches
  Neo4j-has-no-native-datetime + the existing string-prop convention). The executor MAY use a Neo4j
  temporal type (`datetime()`) instead IF it round-trips through `read_graph` without breaking
  `_record_to_node` (test 22 must still pass) — but the string form is the safe default and keeps
  Δt-anchoring parsing simple.
- **Whether `test_handle_end_subgraph_persists_real_neo4j` (test 27) is included** — it is the
  stronger real-infra proof but overlaps test 26's spy assertion. Keep it if the real-container
  persistence proof is wanted; drop it (relying on 26) only if it materially slows the suite. The
  spy test (26) is mandatory; the container variant (27) is the allowed-to-drop one.
- **Default-scope resolution in the CLI** (`MIN(aita_search_spaces.id)`) — the executor MAY require an
  explicit `--search-space-id`/`--concept-id` instead, IF that better matches operational safety
  (refusing an accidental wrong-course rebuild). Either is acceptable; document the choice in the CLI
  docstring.
