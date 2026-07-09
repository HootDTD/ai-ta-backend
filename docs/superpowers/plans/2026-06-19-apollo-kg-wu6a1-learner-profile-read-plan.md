# WU-6A1 — Q1 learner-profile read path (real-PG) — TDD implementation plan

**Date:** 2026-06-19
**Branch (already checked out — do NOT switch/create/push):** `feat/apollo-kg-wu6a1-learner-profile-read`
**diff-cover compare branch:** `feat/apollo-kg-wu5b5-chat-keyword-wireup`
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §1 lines 99-110 (the v1 wedge + Q1 personalization read), §1.4 (per-classroom isolation invariant), §6 Readouts line 470 (Session personalization v1 readout), §12 line 1538 (phase-6 summary).
**Scoping authority:** `docs/superpowers/plans/2026-06-19-apollo-kg-wu6a-split-proposal.md` — WU-6A1 section (§3) + §4 decisions #2/#4/#9 + the ORCHESTRATOR ADJUDICATION.
**Status:** PLAN — code not yet written. This document is the executor prompt.

---

## 0. One-paragraph summary

WU-6A1 builds the genuinely-first READ path over `apollo_learner_state` (the only existing read, `apollo/learner_model/persistence.py:197 _lock_prior_state`, is a `FOR UPDATE` write-lock and is NOT reused). The deliverable is one new pure-IO module `apollo/learner_model/personalization_read.py` exposing two frozen dataclasses (`EntityProfile`, `LearnerProfile`) and one async function `read_learner_profile(db, *, user_id, search_space_id, concept_id) -> LearnerProfile`, plus one new real-PG Testcontainers test file. The read is course-scoped (the §1.4 per-classroom invariant), returns RAW stored columns verbatim (no belief recompute), emits RAW prereq edges + id↔key maps, and returns an empty-but-well-formed profile on the prod cold-start path. NO migration, NO Neo4j, NO LLM.

---

## 1. Scope boundaries (binding)

### In scope — touch ONLY these
- **NEW** `apollo/learner_model/personalization_read.py` — `read_learner_profile` + frozen `LearnerProfile` / `EntityProfile` dataclasses.
- **NEW** `tests/database/test_personalization_read_postgres.py` — real-PG (Testcontainers) acceptance gate.
- **EDIT** `docs/architecture/apollo.md` — drift reconciliation: register `personalization_read.py` + `read_learner_profile` in the `apollo/learner_model/` module-map row; keep `last_verified: 2026-06-19`.

### Explicitly OUT of scope (do NOT touch — frozen upstream)
- `apollo/learner_model/*` belief/update/state_model/decay/negotiation/persistence — the WRITE path. Do NOT add a read to `persistence.py`; do NOT reuse `_lock_prior_state` (it `FOR UPDATE`-locks).
- `apollo/learner_model/belief.py` `mastery_of` / `confidence_of` / `misconception_code_of` — NOT imported, NOT called. Columns are read verbatim.
- `apollo/persistence/*` (ORM models) — consumed read-only, never edited.
- `apollo/persistence/learner_model_seed.py` (`_ENTRY_TYPE_TO_KIND_PREFIX`) — that import belongs to WU-6A2, not here.
- The `prereqs_mastered` SEMANTIC, the `TEACHABLE_BAND_*` / `MASTERED_THRESHOLD` / `UNSEEN_MASTERY` constants — these are WU-6A2's pure golden-vector code (split-proposal §4 decision #4). 6A1 emits RAW edges/maps only and ships NO scoring threshold.
- A future `personalization_select.py` — 6A1 imports NOTHING from it (one-way dependency arrow 6A1 → 6A2; importing back would create a cycle).
- Any migration. `next_free_migration = 030` stays UNUSED.
- Neo4j, any LLM/network call, any flag (`APOLLO_SESSION_PERSONALIZATION_ENABLED` is WU-6A3's).

---

## 2. Ground-truth facts (verified file:line, 2026-06-16/2026-06-19)

- Existing read is a write-lock — do NOT reuse: `apollo/learner_model/persistence.py:186-205` `_lock_prior_state` uses `.with_for_update()`, scopes by a SINGLE `entity_id`. WU-6A1 builds a fresh, no-lock, set-scoped read.
- `apollo_learner_state` ORM = `apollo/persistence/models.py:410-437` `LearnerState`: PK `(user_id UUID, search_space_id Integer, entity_id BigInteger)`; columns `belief` (REAL[3]), `mastery` (Float), `confidence` (Float), `misconception_code` (Text NULL), `evidence_count`, `last_evidence_at`, `updated_at`. WU-6A1 reads ONLY `entity_id, mastery, confidence, misconception_code`.
- `apollo_kg_entities` ORM = `apollo/persistence/models.py:362-389` `KGEntity`: `id` (BigInteger), `concept_id` (FK `apollo_concepts.id`), `canonical_key` (Text), `UNIQUE(concept_id, canonical_key)`. Course ownership flows `concept_id → apollo_concepts → apollo_subjects.search_space_id`.
- `apollo_entity_prereqs` ORM = `apollo/persistence/models.py:392-407` `EntityPrereq`: composite PK `(from_entity_id, to_entity_id)` both BigInteger, FK to `apollo_kg_entities.id` ON DELETE CASCADE. Semantics: **"FROM depends on TO"** — entity E's prerequisites are the `to_entity_id`s where `from_entity_id = E`.
- `is_empty` reflects the PROD path: `apollo_learner_state` is populated ONLY when `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is ON (`done.py` gate), which is OFF in prod — so the table is EMPTY in prod. Cold-start is the PROD path, not an edge case (split-proposal §1 GATING TRUTH).
- Real-PG harness = `tests/conftest.py:119-192`, re-exported to `apollo/` via `apollo/conftest.py:24`: session-scoped `_pg_url` (pgvector/pgvector:pg16 Testcontainer, `Base.metadata.create_all`, Docker-skip-clean) + function-scoped `db_session` (per-test engine, savepoint rollback, `join_transaction_mode="create_savepoint"`). `tests/database/*` files set `pytestmark = pytest.mark.integration`.
- `TEST_USER_ID` / `TEST_USER_ID_2` = `apollo/conftest.py:31-32` (valid UUID strings with non-zero leading hex).
- Direct-ORM seed pattern to REUSE = `tests/database/test_done_layer3_route_postgres.py:79-110` (`_seed_course_with_entity` — `SearchSpace → Subject → Concept → KGEntity`, returns `(search_space_id, concept_id, {canonical_key: entity_id})`). `EntityPrereq` is in `Base.metadata`, so it is a plain `db.add(EntityPrereq(from_entity_id=…, to_entity_id=…))` (mirror `tests/database/test_apollo_learner_model_migration.py:562` INSERT semantics).
- The test harness builds tables via `Base.metadata.create_all` — the migration-026 CHECK constraints are NOT applied unless a test applies them explicitly (the existing layer-3 test applies `_LEARNER_STATE_CHECKS` by hand). WU-6A1 does NOT need the CHECKs (it only READS), so it does not apply them — but every seeded `LearnerState.mastery/confidence` MUST stay within `[0,1]` and `belief` MUST be length-3 anyway (faithful fixtures).

---

## 3. Module to create — `apollo/learner_model/personalization_read.py`

### Public API (frozen — do not deviate)
```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import EntityPrereq, KGEntity, LearnerState


@dataclass(frozen=True)
class EntityProfile:
    """RAW stored columns for one (user, course, entity) learner-state row.
    Read VERBATIM — NO belief recompute (belief.py is NOT imported)."""
    entity_id: int
    canonical_key: str
    mastery: float
    confidence: float
    misconception_code: str | None


@dataclass(frozen=True)
class LearnerProfile:
    by_canonical_key: Mapping[str, EntityProfile]      # present learner_state rows only
    prereq_edges: tuple[tuple[int, int], ...]          # RAW (from_entity_id, to_entity_id)
    entity_id_by_key: Mapping[str, int]
    key_by_entity_id: Mapping[int, str]
    is_empty: bool                                     # True iff ZERO learner_state rows matched


async def read_learner_profile(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
) -> LearnerProfile: ...
```

### Contract decisions (locked, from RECON + split-proposal)
- `prereq_edges` type is `tuple[tuple[int, int], ...]` (RECON CONTRACT), emitted deterministically sorted by `(from_entity_id, to_entity_id)` so the value is stable across reads (the test round-trips it). (The split-proposal text says `frozenset`; the RECON contract — which is binding for THIS unit — says `tuple`. Use the tuple. Both are immutable; the tuple gives a deterministic order for the test.)
- `EntityProfile` carries ONLY raw columns; NO `prereqs_mastered` bool (that semantic is WU-6A2's, split-proposal §4 decision #4).
- `is_empty` reflects the `apollo_learner_state` row count ONLY. On cold-start the maps + edges ARE still populated from `apollo_kg_entities`/`apollo_entity_prereqs`; only `by_canonical_key` is empty.
- `read_learner_profile` imports NOTHING from any `personalization_select` module (one-way arrow). It ships no scoring threshold.

### Algorithm — AT MOST 3 scoped queries, NONE inside a loop (no N+1)
1. **Entities of this concept** (1 query):
   `select(KGEntity.id, KGEntity.canonical_key).where(KGEntity.concept_id == concept_id)`.
   Build `entity_id_by_key` and `key_by_entity_id`. Capture `concept_entity_ids = list(key_by_entity_id)`.
   - If the concept has ZERO entities → return `LearnerProfile({}, (), {}, {}, is_empty=True)` WITHOUT issuing queries 2-3 (an empty `IN ()` is a degenerate query; short-circuit is cleaner and still correct). This is the "unknown concept" guard.
2. **Learner-state rows, course-scoped** (1 query):
   `select(LearnerState.entity_id, LearnerState.mastery, LearnerState.confidence, LearnerState.misconception_code).where(LearnerState.user_id == user_id, LearnerState.search_space_id == search_space_id, LearnerState.entity_id.in_(concept_entity_ids))`.
   **Belt-and-suspenders course scoping:** BOTH the `search_space_id` predicate AND the entity-id restriction to this concept. Build `by_canonical_key` keyed by `key_by_entity_id[entity_id]`. `is_empty = (row count == 0)`.
3. **Prereq edges for this concept's entities** (1 query):
   `select(EntityPrereq.from_entity_id, EntityPrereq.to_entity_id).where(or_(EntityPrereq.from_entity_id.in_(concept_entity_ids), EntityPrereq.to_entity_id.in_(concept_entity_ids)))`.
   Materialize as a `tuple` sorted by `(from_entity_id, to_entity_id)`. Store RAW. 6A1 does NOT compute `prereqs_mastered`.

Return `LearnerProfile(by_canonical_key=…, prereq_edges=…, entity_id_by_key=…, key_by_entity_id=…, is_empty=…)`. All four mapping/tuple fields are immutable snapshots (use `dict`/`tuple` literals; do not hand back live SQLAlchemy buffers).

### Style / safety
- Immutable: build fresh dicts/tuples, never mutate inputs. Frozen dataclasses.
- No mutation of `db`, no `commit`, no `flush` — pure read.
- File well under 800 lines (it is ~70 lines).
- Type annotations on every signature. snake_case, `_at`/`_id` suffixes consistent with neighbors.

---

## 4. Test file to create — `tests/database/test_personalization_read_postgres.py`

**Header conventions (mirror `test_done_layer3_route_postgres.py`):**
- `from __future__ import annotations`
- `pytestmark = pytest.mark.integration` (trips the real-PG gate; MUST run green-not-skipped with Docker up).
- Import `db_session` via the conftest re-export (request the fixture by name — do not import the fixture object).
- Import `TEST_USER_ID`, `TEST_USER_ID_2` from `apollo.conftest`.
- Import ORM `SearchSpace` from `database.models`; `Subject, Concept, KGEntity, EntityPrereq, LearnerState` from `apollo.persistence.models`.
- Import the unit under test: `from apollo.learner_model.personalization_read import read_learner_profile, LearnerProfile, EntityProfile`.

**Local seed helpers (define in-file; adapt `_seed_course_with_entity`):**
- `async def _seed_course(db, *, course_slug) -> tuple[int, int]` — insert `SearchSpace → Subject → Concept`, return `(search_space_id, concept_id)`. (One concept per course; entities are per-concept via the `KGEntity.concept_id` FK.)
- `async def _seed_entity(db, *, concept_id, canonical_key, kind="equation") -> int` — `db.add(KGEntity(...))`, flush, return `ent.id`.
- `async def _seed_prereq(db, *, from_id, to_id)` — `db.add(EntityPrereq(from_entity_id=from_id, to_entity_id=to_id))`, flush.
- `async def _seed_state(db, *, sid, entity_id, mastery, confidence, misconception_code=None, user_id=TEST_USER_ID)` — `db.add(LearnerState(user_id=user_id, search_space_id=sid, entity_id=entity_id, belief=[round(1-mastery,4), 0.0, round(mastery,4)] (a faithful length-3 vector summing≈1 within [0,1]; the read ignores belief but the column is NOT NULL), mastery=mastery, confidence=confidence, misconception_code=misconception_code, evidence_count=1, last_evidence_at=datetime.now(UTC), updated_at=datetime.now(UTC)))`, flush. (Keep `mastery`/`confidence` in `[0,1]`, `belief` length-3.)

### Required tests (each name + assertion + how externals are handled)

External deps: there are NO Neo4j/LLM/network deps on this path — the read is pure Postgres. So NO mocking is required; every test seeds via direct ORM on `db_session` and asserts the returned `LearnerProfile`. (This is the same "no live LLM, hand-seed the rows" discipline as the WU-5A2 layer-3 test, but even simpler — no shadow/grade objects.)

1. **`test_course_isolation_mastery_never_crosses_courses`** (the §1.4 invariant — RECON test 1)
   - Seed course A `(sid_a, cid_a)` and course B `(sid_b, cid_b)`. Under EACH concept seed an entity with the SAME `canonical_key="eq.continuity"` (entities are per-concept, so two rows, one per course; this models "the same concept's entities" shared across courses).
   - Seed a `LearnerState` row in EACH course: course A `mastery=0.42`, course B `mastery=0.91` (distinct sentinel), both for `TEST_USER_ID`, each scoped to its own `(sid, entity_id)`.
   - Call `read_learner_profile(db_session, user_id=TEST_USER_ID, search_space_id=sid_a, concept_id=cid_a)`.
   - Assert `profile.by_canonical_key["eq.continuity"].mastery == 0.42` (course A's value).
   - Assert NO `EntityProfile` in the result carries `mastery == 0.91` (course B's value never leaks): iterate `profile.by_canonical_key.values()` and assert `0.91` absent.
   - Assert `profile.is_empty is False`.
   - **Why it can only pass on real PG:** the dual `search_space_id` + `entity_id.in_(concept-A ids)` scoping is the thing under test; SQLite's lack of the FK chain + the create-all schema make the join semantics worth pinning on PG (matches the repo convention — WU-5A2 isolation runs on PG).

2. **`test_cold_start_empty_state_returns_well_formed_empty_profile`** (the PROD path — RECON test 2)
   - Seed course `(sid, cid)` + TWO entities `e1=eq.continuity`, `e2=cond.incompressibility` + one prereq edge `(e1 -> e2)`. Seed ZERO `apollo_learner_state` rows.
   - Call `read_learner_profile(...)`.
   - Assert `profile.is_empty is True` and `profile.by_canonical_key == {}`.
   - Assert the maps + edges STILL populate (the RECON-recommended "yes"): `profile.entity_id_by_key == {"eq.continuity": e1, "cond.incompressibility": e2}`, `profile.key_by_entity_id == {e1: "eq.continuity", e2: "cond.incompressibility"}`, and `profile.prereq_edges == ((e1, e2),)`.
   - This pins the contract that on cold-start ONLY `by_canonical_key` is empty; the structural maps/edges from `kg_entities`/`prereqs` remain available to 6A2.

3. **`test_column_read_parity_no_belief_recompute`** (RECON test 3)
   - Seed course `(sid, cid)` + entity `eq.bernoulli`. Seed a `LearnerState` row with KNOWN `mastery=0.42`, `confidence=0.70`, `misconception_code="misc.something"` (and a `belief` array that does NOT recompute to those values via `belief.mastery_of` — e.g. `belief=[0.2, 0.6, 0.2]`, whose `mastery_of = 0.5*0.6+0.2 = 0.5 != 0.42` — so a recompute would be DETECTABLY wrong).
   - Call `read_learner_profile(...)`.
   - Assert `profile.by_canonical_key["eq.bernoulli"].mastery == 0.42`, `.confidence == 0.70`, `.misconception_code == "misc.something"`, `.entity_id == <e_id>`, `.canonical_key == "eq.bernoulli"`.
   - **Proves no recompute:** because `0.42 != mastery_of([0.2,0.6,0.2]) == 0.5`, returning `0.42` proves the read takes the COLUMN, not the belief vector. (Use REAL-tolerance compare: `== pytest.approx(0.42, abs=1e-6)` — `mastery`/`confidence` are Postgres `double precision` via the `Float` ORM type, but seeded as exact 0.42/0.70 literals, so exact-or-approx both pass; prefer `pytest.approx`.)

4. **`test_prereq_edges_and_id_key_maps_round_trip`** (RECON test 4 — a small prereq DAG)
   - Seed course `(sid, cid)` + 3 entities `a=eq.continuity`, `b=cond.incompressibility`, `c=eq.bernoulli`. Seed prereq edges modelling "a depends on b" and "c depends on b": `(a -> b)`, `(c -> b)`.
   - Call `read_learner_profile(...)`.
   - Assert `profile.prereq_edges == ((min..),(..))` — specifically `tuple(sorted([(a, b), (c, b)]))` (deterministic order). Assert each raw `(from, to)` tuple is present and the endpoints resolve: for every `(f, t)` in `profile.prereq_edges`, `profile.key_by_entity_id[f]` and `profile.key_by_entity_id[t]` exist.
   - Assert `entity_id_by_key`/`key_by_entity_id` are exact inverses over `{a,b,c}` (`{v: k for k, v in entity_id_by_key.items()} == key_by_entity_id`).
   - Assert `profile.is_empty is True` (no state rows seeded) — confirms edges/maps populate independent of state.

### Branch-coverage tests (needed to clear the 95% patch gate on the module's branches)

5. **`test_unknown_concept_returns_empty_without_state_query`** (the zero-entities short-circuit branch in step 1)
   - Seed course `(sid, cid)` with NO entities under `cid` (concept exists, entity inventory empty). Optionally seed a stray `LearnerState`/entity under a DIFFERENT concept to prove scoping.
   - Call `read_learner_profile(...)`.
   - Assert `profile == LearnerProfile(by_canonical_key={}, prereq_edges=(), entity_id_by_key={}, key_by_entity_id={}, is_empty=True)` (exact frozen-dataclass equality).
   - Exercises the early-return path so it is covered, not just the happy path.

6. **`test_mixed_present_and_absent_entities`** (the split-proposal "Mixed" key test — covers the present/absent partition of `by_canonical_key`)
   - Seed course `(sid, cid)` + 3 entities `eq.continuity`, `cond.incompressibility`, `eq.bernoulli`. Seed a `LearnerState` row for ONLY `eq.continuity` (`mastery=0.35`).
   - Call `read_learner_profile(...)`.
   - Assert `set(profile.by_canonical_key) == {"eq.continuity"}` (present entity carries its column; the two absent entities are simply ABSENT from `by_canonical_key`, NOT zero-filled — 6A2 treats absence as cold).
   - Assert all three keys ARE in `profile.entity_id_by_key` (the inventory is complete even though only one has state).
   - Assert `profile.is_empty is False`.

7. **`test_other_user_state_does_not_leak`** (the `user_id` predicate branch — defense-in-depth alongside course isolation)
   - Seed course `(sid, cid)` + entity `eq.continuity`. Seed a `LearnerState` row for `TEST_USER_ID_2` (`mastery=0.88`) ONLY — none for `TEST_USER_ID`.
   - Call `read_learner_profile(db_session, user_id=TEST_USER_ID, ...)`.
   - Assert `profile.is_empty is True` and `profile.by_canonical_key == {}` (the other user's row is filtered out by the `user_id` predicate — the read is per-student).

**No `xfail`, no `skip`, no assert-nothing.** Every test seeds, calls, and asserts a concrete value. Docker is UP (pgvector:pg16); these MUST run GREEN-not-skipped — a skip is a FAIL of the gate.

---

## 5. How to run the tests (executor) — use the venv interpreter EXPLICITLY

A bare `python` that raises ImportError is an interpreter-selection error, NOT a blocker — switch to the venv interpreter. Real-infra is a write-and-run DELIVERABLE, not a precondition.

```bash
# from ai-ta-backend/ :
.venv/Scripts/python.exe -m pytest tests/database/test_personalization_read_postgres.py -v --tb=short
```
Expect: all 7 tests PASS (green, not skipped). If they skip, Docker is down — start it; do not accept a skip.

Patch-coverage gate (the workflow runs `--cov=.`, so `apollo/learner_model/personalization_read.py` is measured):
```bash
.venv/Scripts/python.exe -m pytest tests/database/test_personalization_read_postgres.py \
    --cov=apollo/learner_model/personalization_read.py --cov-report=xml
.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml \
    --compare-branch=feat/apollo-kg-wu5b5-chat-keyword-wireup --fail-under=95
```
Target: ≥95% patch coverage on the changed lines of `personalization_read.py`. Tests 1-7 between them touch every branch (happy read, cold-start short-circuit, zero-entity short-circuit, present/absent partition, prereq-edge sort, user-scoping, course-scoping).

---

## 6. Owner-doc drift update — `docs/architecture/apollo.md` (same commit)

The doc already declares `owns: [apollo/**, apollo/learner_model/**]` and `last_verified: 2026-06-19` — no `owns:` change, no date bump needed (already today). The required edit is to the `apollo/learner_model/` module-map row (currently ends at the WU-5B2 `negotiation.py` description):

- **Key files list:** add `personalization_read.py` to the row's file enumeration.
- **Role text:** append one sentence registering the new READ seam, e.g.:
  > **WU-6A1 adds `personalization_read.py` — the FIRST read path over `apollo_learner_state` (the existing `_lock_prior_state` is a `FOR UPDATE` write-lock, NOT reused).** `read_learner_profile(db, *, user_id, search_space_id, concept_id) -> LearnerProfile` is a pure, no-lock, course-scoped read (≤3 scoped queries, no N+1): it loads this concept's `apollo_kg_entities` (id↔canonical_key maps), the learner's `apollo_learner_state` rows scoped by BOTH `user_id` AND `search_space_id` AND `entity_id IN (concept entities)` (the §1.4 per-classroom isolation invariant — mastery never crosses courses), and the RAW `apollo_entity_prereqs` edges for those entities. It returns the frozen `LearnerProfile{by_canonical_key (raw EntityProfile columns — mastery/confidence/misconception_code read VERBATIM, NO belief.py recompute), prereq_edges (raw (from,to) tuples), entity_id_by_key, key_by_entity_id, is_empty}`. `is_empty=True` is the PROD cold-start path (the table is empty while `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is OFF) — on cold-start the maps/edges still populate, only `by_canonical_key` is empty. It does NOT compute `prereqs_mastered` (that pure semantic + the band/threshold constants are WU-6A2's `personalization_select.py`) and imports NOTHING from a future `personalization_select` module (one-way dependency arrow, no cycle). No migration. Real-PG tested in `tests/database/test_personalization_read_postgres.py`.

(If a clean place exists, also note WU-6A1 in any "what reads each table" prose for `apollo_learner_state`. Keep the edit minimal and accurate; do not restate WU-6A2/6A3 content that hasn't shipped.)

**Drift-contract check:** confirm `last_verified: 2026-06-19` remains set (it already is). The instruction's "set last_verified to 2026-06-16" is superseded by the doc already carrying today's date 2026-06-19; leaving 2026-06-19 satisfies "bump to today." Do NOT regress the date to 2026-06-16.

---

## 7. TDD execution order (RED → GREEN → verify)

1. **RED:** create `tests/database/test_personalization_read_postgres.py` with all 7 tests + the in-file seed helpers. Import `read_learner_profile` from the not-yet-existing module. Run with the venv interpreter → expect collection/import error (the module doesn't exist) = RED.
2. **GREEN (skeleton):** create `apollo/learner_model/personalization_read.py` with the two frozen dataclasses + `read_learner_profile`. Re-run → all 7 GREEN-not-skipped on real PG.
3. **Refactor/verify:** confirm immutable snapshots (no live ORM buffers leaked), confirm `prereq_edges` deterministic sort, run the diff-cover gate ≥95%.
4. **Drift:** edit `docs/architecture/apollo.md` module-map row (same commit as the code + tests).
5. **Stop.** No commit/push/PR unless separately instructed. No migration. No remote DB touched.

---

## 8. Risks (confidence-rated)

- **LOW — `prereq_edges` container type (tuple vs frozenset).** The split-proposal §3 text says `frozenset`; the binding RECON CONTRACT for THIS unit says `tuple[tuple[int,int],...]`. RESOLVED: use the RECON `tuple`, sorted deterministically. 6A2 consumes it by membership/iteration either way; the tuple's deterministic order makes the 6A1 test exact. Confidence: HIGH this is the right call for 6A1.
- **LOW — empty `IN ()` query.** A concept with zero entities would make `entity_id.in_([])` a degenerate/always-false predicate. RESOLVED by the step-1 short-circuit (test 5 pins it). Confidence: HIGH.
- **LOW — REAL vs double precision on `mastery`/`confidence`.** The ORM column is `Float` (Postgres `double precision`), seeded with exact literals (0.42/0.70). Compare with `pytest.approx(..., abs=1e-6)` to be robust; exact `==` also passes for these literals. Confidence: HIGH.
- **LOW — `belief` NOT NULL on seeded rows.** `LearnerState.belief` is `nullable=False`; the seed helper MUST supply a length-3 list. RESOLVED in the `_seed_state` helper (length-3, in-range, NOT recomputing to the seeded mastery so test 3 stays discriminating). Confidence: HIGH.
- **LOW — Docker availability.** The gate REQUIRES Docker up (pgvector:pg16). If `db_session` skips, the run is INCOMPLETE, not passing. Mitigation: run with the venv interpreter and confirm green-not-skipped; treat a skip as a failure to fix (start Docker), per the binding constraint. Confidence: HIGH Docker is up.
- **LOW — owner-doc date instruction conflict.** The task says set `last_verified` to 2026-06-16 in one place and 2026-06-19 in another; the doc already carries 2026-06-19 (today). RESOLVED: keep 2026-06-19 (today's date is the drift-contract requirement — "bump to today"). Confidence: HIGH.

---

## 9. Deviations the executor MAY make without re-planning

- Rename in-file test seed helpers / add small additional positive assertions, as long as every RECON-required behavior (course isolation, cold-start, column parity, prereq-edge + maps round-trip) is asserted on real PG.
- Choose `or_(...)` vs two `union`-ed selects for the prereq query, as long as it stays ONE query and emits the same RAW edge set.
- Add `# pragma: no cover` ONLY to a genuinely unreachable defensive line (none expected); never to dodge a real branch.
- Add `__all__` to the new module.

## 10. Deviations the executor MUST NOT make
- Do NOT reuse/extend `_lock_prior_state` or add any `FOR UPDATE`/lock.
- Do NOT import or call `belief.mastery_of`/`confidence_of`/`misconception_code_of` (no recompute).
- Do NOT add the `prereqs_mastered` semantic, scoring constants, or any import from a `personalization_select` module.
- Do NOT edit `apollo/learner_model/*` (other files), `apollo/persistence/*`, or add a migration.
- Do NOT switch/create branches, push, or open a PR. Do NOT touch any remote DB.
- Do NOT `skip`/`xfail` the real-PG tests or weaken them to assert-nothing.
