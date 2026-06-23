# Plan: WU-6A2 — pure selection + coverage + difficulty algorithm

**Goal:** Land `apollo/learner_model/personalization_select.py` — the PURE (no-IO) selection algorithm that, given a frozen `LearnerProfile` (WU-6A1) + a candidate `Problem` pool, picks the problem whose reference graph best covers the student's weak-and-teachable entities; cold-start / empty-weak → today's `candidates[0]` byte-identical.
**Architecture:** ONE new pure module + ONE new golden-vector test file inside `apollo/learner_model/`, plus the `apollo.md` owner-doc drift edit. No DB, no LLM, no Neo4j, no migration, no live wiring (that is WU-6A3).
**Tech stack:** Python 3.11+, pydantic v2 (`apollo.schemas.problem.Problem`), pytest (`asyncio_mode=auto`, `--strict-markers`), `.venv/Scripts/python.exe`, diff-cover ≥95% vs `feat/apollo-kg-wu6a1-learner-profile-read`.

---
provides:
  - apollo/learner_model/personalization_select.py — TEACHABLE_BAND_LO/HI, MASTERED_THRESHOLD, UNSEEN_MASTERY, REPROBE_CONFIDENCE; reference_entity_keys, prereqs_mastered, weak_teachable, coverage_score, personalize_selection (consumed by WU-6A3 wiring)
consumes:
  - apollo.learner_model.personalization_read (LearnerProfile, EntityProfile — FROZEN WU-6A1)
  - apollo.persistence.learner_model_seed._ENTRY_TYPE_TO_KIND_PREFIX (FROZEN WU-3B map — imported, never mutated)
  - apollo.schemas.problem.Problem / ReferenceStep
  - apollo.errors.PoolExhaustedError
depends_on:
  - WU-6A1 (already landed on this branch: apollo/learner_model/personalization_read.py)
  - diff-cover compare branch: feat/apollo-kg-wu6a1-learner-profile-read
---

## Overview

WU-6A2 is the **pure scoring brain** of the v1 Apollo session-personalization wedge. It is the second of three v1 sub-units (6A1 read / **6A2 pure-selection** / 6A3 wiring). It owns EVERY number and the cold-start branch; it touches NO IO. The dependency arrow is strictly one-way: **6A2 imports the frozen 6A1 dataclasses; 6A1 imports nothing from 6A2** (no import cycle).

The single non-obvious design fact, EMPIRICALLY PROVEN against the real interpreter on 2026-06-19 (`.venv/Scripts/python.exe`):

- `Problem.model_validate(payload)` DROPS the seeded per-step `entity_key` (pydantic v2 `extra` defaults to `ignore`; `hasattr(parsed_step, "entity_key") == False`).
- BUT each problem's canonical-key set is a PURE deterministic function of `(entry_type, id)` via the FROZEN `_ENTRY_TYPE_TO_KIND_PREFIX` map (`learner_model_seed.py:80-86`) and the same `f"{prefix}.{id}"` rule the seeder used (`learner_model_seed.py:198-199`).
- VERIFIED three ways on `problem_01`: `reference_entity_keys(parsed) == {spec.canonical_key for spec in reference_solution_to_entities(raw)} == {step["entity_key"] for step in raw["reference_solution"]} == True`.

⇒ `reference_entity_keys` reconstructs each candidate's entity-key set IN-MEMORY from the already-loaded pool. **Zero per-problem DB/Neo4j round-trip — the N+1 hazard does not exist.** Test E makes this concrete with a real bernoulli problem.

All ten orchestrator decisions are ADJUDICATED (split proposal §4 decisions 1-10, §OPEN-DECISIONS): the constants and rules below are DECIDED, not open. This plan does not re-open them.

## Prior art (sibling modules)

- **DI / no-DI seam, pure-library convention:** `apollo/learner_model/belief.py` (WU-5A1) — a pure module: `import math`, NAMED module constants (`COLD_START_PRIOR`, `GAMMA`, `LIKELIHOOD_FLOOR`), free functions, no IO. `personalization_select.py` mirrors it exactly (stdlib `set`/`dict`/`sorted`/`max` only).
- **Pure golden-vector test convention:** `apollo/learner_model/tests/test_belief.py:1-55` — module header states "Pure imports — no DB, no LLM, no Neo4j, no network. Every expected number is HAND-COMPUTED … NEVER asserted against the function's own output." A `test_locked_constants` pins every constant literally. `test_personalization_select.py` mirrors this.
- **Package-seam + no-IO contract pin:** `apollo/learner_model/tests/test_package_seam.py:38,93-99` — `_IO_TOKENS = ("sqlalchemy", "asyncpg", "openai", "neo4j")` and `test_no_io_imports` asserts none appear in the pure module sources via `inspect.getsource`. **NOTE:** that file's `test_public_api_importable_from_package_root` pins the package-root `__all__` to the EXACT WU-5A1 set. We do NOT re-export `personalization_select` through `apollo/learner_model/__init__.py` (6A1's `personalization_read` is itself not re-exported there — confirmed by reading `__init__.py`), so `test_package_seam.py` stays GREEN untouched. 6A2 imports its inputs from the SUBMODULE (`apollo.learner_model.personalization_read`), never the package root.
- **Byte-identity reference (the non-regression anchor):** `apollo/overseer/problem_selector.py:42-59` `select_problem` — `candidates = [p for p in pool if p.difficulty == difficulty and p.id not in attempted]`; `if not candidates: raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)`; `return candidates[0]`. `personalize_selection`'s candidate filter, raise, and cold-start branch are byte-identical to these three lines.
- **Frozen DTO source (WU-6A1, already landed):** `apollo/learner_model/personalization_read.py:42-73` — `EntityProfile{entity_id:int, canonical_key:str, mastery:float, confidence:float, misconception_code:str|None}` and `LearnerProfile{by_canonical_key: Mapping[str,EntityProfile], prereq_edges: tuple[tuple[int,int],...], entity_id_by_key: Mapping[str,int], key_by_entity_id: Mapping[int,str], is_empty: bool}`. `prereq_edges` is "FROM depends on TO" and WITHIN-CONCEPT (every endpoint resolves through the id↔key maps).
- **Frozen prefix map (WU-3B):** `apollo/persistence/learner_model_seed.py:80-86` `_ENTRY_TYPE_TO_KIND_PREFIX` (`equation→("equation","eq")`, `condition→("condition","cond")`, `simplification→("condition","simp")`, `procedure_step→("procedure","proc")`, `definition→("definition","def")`). The PREFIX is element `[1]` of the tuple. `learner_model_seed.py` has NO `sqlalchemy` import (it is the pure conversion core), so importing from it does not pull IO into the pure module — VERIFY in the no-IO test.

## Structural prep (from neighborhood scan)

Neighborhood scanned (the change path is exactly TWO new files; one ring out = their direct imports):

- `personalization_select.py` direct imports: `apollo.learner_model.personalization_read` (2 dataclasses), `apollo.persistence.learner_model_seed` (1 constant), `apollo.schemas.problem` (`Problem`), `apollo.errors` (`PoolExhaustedError`), stdlib (`collections.abc.Sequence`/`Mapping`, `dataclasses`-none, `__future__`). **5 first-party imports — under the >8 CBO flag.**
- Exported names: 5 constants + 5 functions = **10 public symbols — under the >20 WMC flag.** (Internal helper `_mastery_of_key` is private.)
- Circular-import check: `personalization_read.py` imports only `sqlalchemy` + `apollo.persistence.models`; `learner_model_seed.py` imports neither. `personalization_select` is imported by NOTHING in 6A2's scope (6A3 will import it later). **No cycle possible.**
- Coupling fan-in of the targets: zero today (both files are new).

**None — neighborhood is clean.** No structural prep precedes the feature work.
- Verify: `.venv/Scripts/python.exe -c "import ast,apollo.learner_model.personalization_select as m; print(len([n for n in dir(m) if not n.startswith('_')]))"` (expect 10 after build).

## Layered tasks (ORDER MATTERS — TDD)

> The classic 7-layer order (migration → repository → DTO → service → controller → wiring → tests) is mapped onto this PURE unit below. Most layers are JUSTIFIED-ABSENT (this is a no-IO library, not an HTTP feature). The load-bearing order is: **write the failing tests FIRST (step 7), then the module (step 4).**

### 1. DB migration — JUSTIFIED ABSENT
- [ ] No migration. WU-6A2 is a pure in-memory algorithm over the frozen migration-026 tables that WU-6A1 already reads. `next_free_migration` stays unused. NEVER apply migrations to any remote DB.
- Verify: no file under `database/migrations/` is created or edited (the REAL-INFRA gate must NOT trip).

### 2. Repository / data-access — JUSTIFIED ABSENT
- [ ] No repository. The data-access layer is WU-6A1's `read_learner_profile` (already landed). 6A2 receives the `LearnerProfile` + `Problem` pool as IN-MEMORY parameters; it issues no query. No file under `tests/database/`, `apollo/knowledge_graph/`, or `apollo/persistence/neo4j_client.py` is touched.
- Verify: `grep -nE "sqlalchemy|asyncpg|AsyncSession|select\(|neo4j|OpenAI" apollo/learner_model/personalization_select.py` returns nothing.

### 3. DTOs — JUSTIFIED ABSENT (consumes frozen 6A1 DTOs)
- [ ] No new DTO. Inputs are the FROZEN `LearnerProfile`/`EntityProfile` (WU-6A1) and `Problem` (pydantic, frozen schema). Output is a `Problem`. Internal `weak_teachable` returns a plain `dict[str, float]` (not a dataclass — matches `belief.py`'s tuple-returning style; it is an internal scoring intermediate, never persisted).

### 4. The pure module (personalization_select.py) — the "service" layer

- [ ] File (NEW): `apollo/learner_model/personalization_select.py`
- Module header: PURE — no DB/LLM/Neo4j/containers; one-way arrow (imports 6A1, 6A1 imports nothing back); the proven no-N+1 reconstruction; "6A2 owns every number"; all constants orchestrator-adjudicated.
- Imports (EXACT):
  ```python
  from __future__ import annotations
  from collections.abc import Mapping, Sequence
  from apollo.errors import PoolExhaustedError
  from apollo.learner_model.personalization_read import EntityProfile, LearnerProfile
  from apollo.persistence.learner_model_seed import _ENTRY_TYPE_TO_KIND_PREFIX
  from apollo.schemas.problem import Problem
  ```
  (`EntityProfile` is imported for type hints / test re-use even though only `LearnerProfile` is dereferenced — keep it for the public seam; if a linter flags it unused, hint it in `__all__` or drop it. The executor MAY drop `EntityProfile` from the import if unused — see Deviations.)
- `__all__` lists the 10 public symbols (5 constants + `reference_entity_keys`, `prereqs_mastered`, `weak_teachable`, `coverage_score`, `personalize_selection`).

- **CONSTANTS (module-level, named; all orchestrator-adjudicated — split proposal §4):**
  ```python
  TEACHABLE_BAND_LO = 0.3      # spec §6 line 470 "mastery 0.3–0.7" (inclusive)
  TEACHABLE_BAND_HI = 0.7
  MASTERED_THRESHOLD = 0.7     # prereq "mastered" cut (reuse band top — one fewer magic number)
  UNSEEN_MASTERY = 0.50        # readout for an entity with no learner_state row (matches belief cold-start)
  REPROBE_CONFIDENCE = 0.4     # low-confidence re-probe threshold (soft-hold; never a hard negative)
  ```

- **`reference_entity_keys(problem: Problem) -> frozenset[str]`**
  - `return frozenset(f"{_ENTRY_TYPE_TO_KIND_PREFIX[s.entry_type][1]}.{s.id}" for s in problem.reference_solution)`
  - The PROVEN round-trip. Element `[1]` of the prefix tuple is the prefix string. Imports the frozen map; never mutates it.

- **`_mastery_of_key(profile: LearnerProfile, key: str) -> float`** (private)
  - `ep = profile.by_canonical_key.get(key); return ep.mastery if ep is not None else UNSEEN_MASTERY`
  - The SINGLE place the "unseen = 0.50" rule lives. (Use `.get` not `in`+index for immutability/clarity.)

- **`prereqs_mastered(profile: LearnerProfile, key: str) -> bool`**
  - prereq DIRECTION: `apollo_entity_prereqs` is "FROM depends on TO", so E's prereqs are the TO-endpoints of edges whose FROM is E.
  - `entity_id = profile.entity_id_by_key.get(key)` — if `key` not in the id map (defensive; should not happen for a present key) treat as no-prereqs → `True`.
  - `prereq_ids = [to for (frm, to) in profile.prereq_edges if frm == entity_id]`
  - `prereq_keys = [profile.key_by_entity_id[pid] for pid in prereq_ids]` (every endpoint resolves — 6A1 guarantees within-concept edges).
  - `return all(_mastery_of_key(profile, pk) >= MASTERED_THRESHOLD for pk in prereq_keys)` — an entity with NO prereq edges is trivially `True` (`all([]) == True`). An UNSEEN prereq → `_mastery_of_key == 0.50 < 0.70` → blocks (adjudication #3, conservative).

- **`weak_teachable(profile: LearnerProfile) -> dict[str, float]`**
  - `return {ep.canonical_key: 1.0 - ep.mastery for ep in profile.by_canonical_key.values() if TEACHABLE_BAND_LO <= ep.mastery <= TEACHABLE_BAND_HI and prereqs_mastered(profile, ep.canonical_key)}`
  - Only PRESENT entities can be weak (absent entities have no signal). Deficit = `1 - mastery`. Band is INCLUSIVE both ends. SOFT-HOLD re-probe: low `confidence` does NOT exclude (adjudication #5a — never a hard negative). On a cold-start profile `by_canonical_key == {}` ⇒ returns `{}`.

- **`coverage_score(problem: Problem, weak: Mapping[str, float]) -> float`**
  - `return sum(weak[k] for k in reference_entity_keys(problem) if k in weak)` — deficit-weighted (adjudication #4). Empty intersection → `0.0`.

- **`personalize_selection(profile, pool, *, concept_id: int, difficulty: str, attempted_ids) -> Problem`**
  - Signature: `(profile: LearnerProfile, pool: Sequence[Problem], *, concept_id: int, difficulty: str, attempted_ids: Sequence[str]) -> Problem`
  - STEP 1 (byte-identical filter): `attempted = set(attempted_ids); candidates = [p for p in pool if p.difficulty == difficulty and p.id not in attempted]` — preserves the `sorted-by-Problem.id` order the pool arrives in (clamp-within-choice: re-rank only inside the chosen difficulty; anti-starvation not-attempted filter PRESERVED).
  - STEP 2 (byte-identical raise): `if not candidates: raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)` — `concept_id` is in the signature SOLELY for this byte-identical reconstruction; it is NOT used in scoring.
  - STEP 3 (cold-start / empty-weak fallback = `select_problem`'s exact behavior): `weak = weak_teachable(profile); if profile.is_empty or not weak: return candidates[0]`.
  - STEP 4 (deficit-weighted pick, tie-break lowest `Problem.id`): `return max(candidates, key=lambda p: (coverage_score(p, weak), _neg_id_sort_key(p)))` — OR, clearer and LOCKED to today's stability: compute `best = max(coverage_score(p, weak) for p in candidates)`, then `return min((p for p in candidates if coverage_score(p, weak) == best), key=lambda p: p.id)`. The executor picks ONE form; the test pins behavior (highest score, ties → lowest `Problem.id`). Because the pool arrives `sorted(key=p.id)`, "lowest id among max-score" == today's `candidates[0]` when the weak set is empty (the non-regression degeneracy).
  - **NEVER use Python `max`'s "first max wins" on an UNSORTED list as the tie rule** — make the lowest-`Problem.id` tie-break EXPLICIT so it is independent of pool order. Recompute or memoize `coverage_score` per candidate (a 4-element pool; recompute is fine and keeps the function pure/stateless).
- Verify: `.venv/Scripts/python.exe -c "import apollo.learner_model.personalization_select"` imports clean.

### 5. Controller / wiring — JUSTIFIED ABSENT (this is WU-6A3)
- [ ] No wiring. 6A2 does NOT touch `problem_selector.py`, `next.py`, `session_init.py`, any flag, or `draft_reply`. The live wiring of `personalize_selection` into the two `select_problem` call-sites behind `APOLLO_SESSION_PERSONALIZATION_ENABLED` is WU-6A3. Wiring here would mis-scope the unit and trip the real-infra gate.

### 6. Module wiring / package seam

- [ ] Do NOT add `personalization_select` to `apollo/learner_model/__init__.py` `__all__`. The package-root `__all__` is PINNED by `test_package_seam.py:44-65` to the exact WU-5A1 set; adding a name there would turn that pre-existing test RED. WU-6A1's `personalization_read` is likewise absent from the package root (verified), so the convention is: personalization modules are imported by SUBMODULE path, never re-exported. The module's OWN `__all__` (10 symbols) is the public seam WU-6A3 imports via `from apollo.learner_model.personalization_select import personalize_selection, ...`.
- [ ] No `apollo/conftest.py` change — the pure test needs no fixtures (no `db_session`, no `neo4j_client`). It loads seed JSON via a filesystem path helper (see test 7E).
- Verify: `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_package_seam.py -q` stays GREEN (no edit to that file or `__init__.py`).

### 6b. Owner-doc drift reconciliation (SAME COMMIT — drift contract)

- [ ] File (EDIT): `docs/architecture/apollo.md`
- The `apollo/learner_model/` module-map row is **line 41**; it already documents WU-6A1 (`personalization_read.py`). **APPEND** a WU-6A2 paragraph to the END of that same cell (after the WU-6A1 `personalization_read` sentence), and **ADD `personalization_select.py`** to the row's file list (cell 2: `belief.py, state_model.py, update.py, persistence.py, decay.py, negotiation.py, personalization_read.py, ..., __init__.py`).
- Suggested WU-6A2 paragraph (concise, interface + data-flow + the load-bearing facts): *"**WU-6A2 adds `personalization_select.py` — the PURE (no-IO) selection + coverage + difficulty algorithm** (mirrors `belief.py`; stdlib `set`/`dict`/`sorted` only, NO DB/LLM/Neo4j/container/migration). It CONSUMES the frozen WU-6A1 `LearnerProfile` + an in-memory candidate `Problem` pool and returns the `Problem` whose reference graph best covers the student's weak-and-teachable entities. NAMED constants (6A2 owns every number, orchestrator-adjudicated): `TEACHABLE_BAND_LO=0.3`/`TEACHABLE_BAND_HI=0.7` (the spec §6 L470 teachable band, inclusive), `MASTERED_THRESHOLD=0.7` (prereq-mastered cut), `UNSEEN_MASTERY=0.50` (an entity with no learner_state row), `REPROBE_CONFIDENCE=0.4` (low-confidence soft-hold). `reference_entity_keys(problem)` reconstructs each candidate's canonical-key set IN-MEMORY via the FROZEN `_ENTRY_TYPE_TO_KIND_PREFIX` (imported from `learner_model_seed`) — PROVEN identical to the seeded `canonical_key`s, so there is NO per-problem DB/Neo4j round-trip (NO N+1; the parsed `Problem` drops the seeded `entity_key`). `prereqs_mastered(profile,key)` derives the prereq gate purely from 6A1's RAW `prereq_edges` (FROM-depends-on-TO) + id↔key maps — an UNSEEN prereq (0.50<0.70) BLOCKS (conservative). `weak_teachable(profile)` = present entities in `[0.3,0.7]` with mastered prereqs (deficit `1-mastery`; low confidence never excludes — soft re-probe). `coverage_score(problem,weak)` = deficit-weighted sum over covered weak entities. `personalize_selection(profile,pool,*,concept_id,difficulty,attempted_ids)` filters to the in-difficulty unattempted candidates (clamp-within-choice; anti-starvation filter preserved), raises the BYTE-IDENTICAL `PoolExhaustedError(concept_cluster_id=str(concept_id),difficulty=…)` when none remain, returns `candidates[0]` on a cold-start/empty-weak profile (the non-regression anchor == today's `select_problem`), else the max-coverage candidate, tie-break lowest `Problem.id`. It imports NOTHING from 6A1 in reverse (one-way arrow, no cycle) and is NOT re-exported through the package root (`__init__.py` `__all__` stays the WU-5A1 set); WU-6A3 imports it by submodule path. Pure golden-vector tested in `apollo/learner_model/tests/test_personalization_select.py` (constants; 3-way round-trip parity on all 5 seed problems; the band/prereq/soft-hold predicates; deficit-weighted pick + lowest-id tie-break + cold-start fallback + `PoolExhaustedError` byte-identity + `standard` single-candidate degeneracy)."*
- [ ] Bump frontmatter `last_verified` (line 13) to **`2026-06-19`** (already today's value — re-affirm it; this is the binding-constraint date for this unit). NOTE: the task brief's drift clause mentions "2026-06-16" in one place and "2026-06-19" in another — 2026-06-19 is correct (today; matches the binding constraint and the existing doc value). Surfaced as a signal.
- [ ] No `owns:` change — `apollo.md` already declares `apollo/learner_model/**`.
- Verify: `grep -n "personalization_select" docs/architecture/apollo.md` returns the new mentions; frontmatter `last_verified: 2026-06-19`.

### 7. Tests (written FIRST — RED before GREEN)

- [ ] File (NEW): `apollo/learner_model/tests/test_personalization_select.py` — alongside `test_belief.py`/`test_decay.py`/`test_package_seam.py`. Pure golden-vector unit. NO `db_session`, NO `neo4j_client`, NO live API, NO skip/xfail/assert-nothing. Mark every test `@pytest.mark.unit` (registered marker; `--strict-markers` is on). Every expected number is HAND-COMPUTED from the worked examples below and pinned as a literal — NEVER asserted against the function's own output.

**Test-file header** mirrors `test_belief.py`: states PURE (no DB/LLM/Neo4j/network), constants are LOCKED golden vectors, all difficulty-discriminating cases pin `difficulty="intro"` (the seed has 4 intro / 1 standard / 0 hard).

**Shared builders (module-level helpers in the test file):**
- `_ep(key, mastery, *, entity_id, confidence=0.8, code=None) -> EntityProfile` — construct an `EntityProfile` directly.
- `_profile(entries, *, prereq_edges=(), is_empty=None) -> LearnerProfile` — build `by_canonical_key` from a list of `EntityProfile`, derive `entity_id_by_key`/`key_by_entity_id` from them (and from any extra ids referenced by `prereq_edges`), default `is_empty` to `not entries`. Lets every test construct a profile with NO DB.
- `_problem(pid, difficulty, keys) -> Problem` — build a `Problem` whose `reference_solution` is a list of `ReferenceStep` chosen so `reference_entity_keys` yields exactly `keys`. To get key `"eq.continuity"` use `entry_type="equation", id="continuity"`; `"cond.incompressibility"` → `condition`/`incompressibility`; `"proc.X"` → `procedure_step`/`X`; `"simp.X"` → `simplification`/`X`; `"def.X"` → `definition`/`X`. **Procedure steps need `content={"order": n, "action": "...", "purpose": "...", "uses_equations": [<an equation id in this problem>]}`** to pass `Problem`'s `_resolve_references` validator (order must be 1..N contiguous; `uses_equations` must reference a real equation id in the same problem; `depends_on` must resolve). Equation/condition/simplification steps take `content={"label": "..."}` and `depends_on=[]`. Keep fixtures minimal-but-VALID; if building a many-`proc` problem is fiddly, prefer `Problem.model_validate` on a hand-written dict or load the REAL seed JSON (test E does the latter).
- `SEED_DIR = Path(apollo.__file__).parent / "subjects/fluid_mechanics/concepts/bernoulli_principle/problems"` — for the real-data tests (E and the pool tests).

**The locked golden tests (each name → what it asserts → how deps are handled):**

1. `test_locked_constants` — assert the 5 constants literally: `TEACHABLE_BAND_LO == 0.3`, `TEACHABLE_BAND_HI == 0.7`, `MASTERED_THRESHOLD == 0.7`, `UNSEEN_MASTERY == 0.50`, `REPROBE_CONFIDENCE == 0.4`. (Mirrors `test_belief.py::test_locked_constants`.) No deps.

2. `test_reference_entity_keys_constructed` — build `_problem("p1","intro", {"eq.continuity","cond.incompressibility"})` and assert `reference_entity_keys(p) == frozenset({"eq.continuity","cond.incompressibility"})`. Proves the prefix-map reconstruction on a constructed problem. No deps.

3. `test_reference_entity_keys_roundtrip_parity_all_seed_problems` **(Example E — the make-or-break N+1 disproof)** — for EACH of the 5 real seed files: load `raw = json.load(open(SEED_DIR/f"problem_0{n}.json"))`, parse `p = Problem.model_validate(raw)`, assert `reference_entity_keys(p) == {spec.canonical_key for spec in reference_solution_to_entities(raw)}` AND `== {step["entity_key"] for step in raw["reference_solution"]}`. Pin `problem_01`'s expected set as a LITERAL frozenset too (`{cond.incompressibility, eq.bernoulli, eq.continuity, proc.plan_apply_continuity, proc.plan_apply_horizontal_simplification, proc.plan_solve_bernoulli_for_p2, simp.horizontal_simplification}`). Deps: imports the FROZEN `reference_solution_to_entities` (pure, DB-free) + reads files off disk — NO DB, NO Neo4j, NO N+1. This is the concrete proof that the in-memory reconstruction equals the seeded canonical_keys.

4. `test_prereqs_mastered_no_edges_is_true` — `prereqs_mastered(_profile([_ep("eq.x", 0.4, entity_id=10)]), "eq.x") == True` (no prereq edges → `all([])`). No deps.

5. `test_prereqs_mastered_unseen_prereq_blocks` **(Example D-blocked)** — profile: `eq.continuity` (id 1, mastery 0.40), edge `(1, 2)` meaning continuity depends-on entity 2 (`cond.incompressibility`), but entity 2 has NO `EntityProfile` row (unseen) and IS in the id↔key maps. `prereqs_mastered(profile, "eq.continuity") == False` (unseen → 0.50 < 0.70). No deps.

6. `test_prereqs_mastered_mastered_prereq_admits` **(Example D-admitted)** — same edge `(1,2)` but entity 2 (`cond.incompressibility`) present with mastery 0.80 ≥ 0.70 → `prereqs_mastered(profile, "eq.continuity") == True`. No deps.

7. `test_weak_teachable_band_inclusive_and_deficit` — entities: `eq.continuity` mastery 0.35 (deficit 0.65, in-band, no prereqs), `eq.bernoulli` mastery 0.68 (deficit 0.32, in-band), `eq.solved` mastery 0.90 (out of band, excluded), `eq.lo` mastery 0.30 (LOWER bound inclusive → included, deficit 0.70), `eq.hi` mastery 0.70 (UPPER bound inclusive → included, deficit 0.30). Assert `weak_teachable(profile)` == the exact dict of the 4 in-band keys with their deficits (0.30/0.70 boundaries present; 0.90 absent). HAND-COMPUTE each deficit. No deps.

8. `test_weak_teachable_excludes_prereq_blocked` — `eq.continuity` mastery 0.40 in-band but its only prereq (`cond.incompressibility`, unseen) blocks → `weak_teachable(profile) == {}`. (The exclusion that drives the fallback.) No deps.

9. `test_weak_teachable_softhold_keeps_low_confidence` **(Example G — soft-hold)** — `eq.continuity` mastery 0.40 in-band, prereqs ok, `confidence=0.2 < REPROBE_CONFIDENCE`. Assert it is STILL in `weak_teachable(profile)` with deficit 0.60 (low confidence is never a hard negative — adjudication #5a). No deps.

10. `test_weak_teachable_empty_on_cold_start` — `weak_teachable(_profile([], is_empty=True)) == {}` (empty `by_canonical_key`). No deps.

11. `test_coverage_score_deficit_weighted` — `weak = {"eq.continuity": 0.65, "cond.incompressibility": 0.60, "eq.bernoulli": 0.32}`; P1 covers all three → `coverage_score(P1, weak) == pytest.approx(1.57)`; a problem covering only `{eq.bernoulli}` → `0.32`; a problem covering `{}` → `0.0`. Build P1/P3/P2/P4 via `_problem` with the worked-example key sets. Use `pytest.approx` for float sums. No deps.

12. `test_personalize_selection_deficit_weighted_pick` **(Example A — the discriminating case, difficulty="intro")** — pool = [P1, P2, P3, P4] (the 4 intro key sets from the worked examples), profile weak: `eq.continuity` 0.35, `cond.incompressibility` 0.40, `eq.bernoulli` 0.68 (all in-band, prereqs ok). Scores: P1=1.57, P3=1.25, P2=0.32, P4=0.00. Assert `personalize_selection(profile, pool, concept_id=7, difficulty="intro", attempted_ids=[]).id == "bernoulli_horizontal_pipe_find_p2"` (P1). Construct the 4 problems via `_problem` (or load the real seed files via `model_validate`). No deps.

13. `test_personalize_selection_tie_break_lowest_id` **(Example B — tie → lowest Problem.id)** — weak = only `eq.continuity` deficit 0.50. P1 and P3 both cover it → tie at 0.50; P2/P4 → 0.00. Assert the returned `.id` is `min("bernoulli_horizontal_pipe_find_p2", "continuity_area_change_find_v2")` = `"bernoulli_horizontal_pipe_find_p2"` (string-min; HAND-COMPUTE which sorts first). Run twice and assert IDENTICAL (determinism, independent of pool order — also pass the pool reversed and assert same result). No deps.

14. `test_personalize_selection_cold_start_returns_candidates0` **(Example C — the non-regression anchor)** — `is_empty=True` profile, pool = 4 intro problems passed IN-ORDER as the sorted-by-id pool (`list_problems_for_concept` always returns `sorted(key=p.id)`). Assert `personalize_selection(profile, pool, concept_id=7, difficulty="intro", attempted_ids=[]).id == sorted(pool, key=lambda p:p.id)[0].id`. **VERIFIED (interpreter, 2026-06-19): the sorted-first intro id is `"bernoulli_height_change_find_v2"` (P2, alphabetical), NOT P1 — do NOT hardcode P1 as `candidates[0]`; compute it via `sorted(...)[0]` or pin the literal `"bernoulli_height_change_find_v2"`.** Then ALSO assert it equals the candidate `select_problem` would return: replicate `select_problem`'s pure filter `[p for p in sorted(pool,key=lambda p:p.id) if p.difficulty=="intro" and p.id not in set()][0]` and assert same `.id`. (Pure replication — do NOT call the async `select_problem`; pin byte-identity of the BRANCH.) No deps.

15. `test_personalize_selection_nonempty_profile_empty_weak_returns_candidates0` — non-empty profile where every present entity is >0.7 OR prereq-blocked ⇒ `weak == {}` ⇒ `candidates[0]`. Distinct from the `is_empty` branch (this exercises the `or not weak` half of STEP 3). No deps.

16. `test_personalize_selection_partially_warm_unseen_prereq_blocks_to_fallback` **(Example D end-to-end)** — profile: only in-band entity is `eq.continuity` (0.40) whose prereq `cond.incompressibility` is UNSEEN → blocked → `weak == {}` → returns `candidates[0]`. Then the ADMITTED variant: same but prereq present at 0.80 → `eq.continuity` enters weak → P1/P3 (which cover it) win over P2/P4; assert the returned id is the coverage winner (lowest-id of {P1,P3} on a tie), NOT `candidates[0]` if they differ. Two asserts, one test (the blocked/admitted pair). No deps.

17. `test_personalize_selection_pool_exhausted_byte_identical` **(Example F)** — pool = the 4 intro + 1 standard problems but request `difficulty="hard"` (zero candidates in the seed mix). Assert `pytest.raises(PoolExhaustedError)` and inside, assert `exc.value.concept_cluster_id == "7"` (== `str(concept_id)` for `concept_id=7`) AND `exc.value.difficulty == "hard"` AND `str(exc.value) == "Problem pool exhausted for cluster '7' at difficulty 'hard'"` (byte-identical to `errors.py:67-70`). Also a second case: all `intro` candidates in `attempted_ids` → raises identically. No deps.

18. `test_personalize_selection_standard_single_candidate_degeneracy` — at `difficulty="standard"` the seed has exactly ONE candidate (P5 `bernoulli_full_find_p2`); a non-empty weak profile still returns P5 (personalization cannot differ with one candidate). Guards against a vacuous "differs at standard" assertion. No deps.

19. `test_personalize_selection_respects_attempted_ids_filter` — pool = 4 intro; put the would-be winner (P1) in `attempted_ids`; assert the result is the BEST of the remaining (anti-starvation filter preserved + re-ranked within remaining). HAND-COMPUTE the runner-up. No deps.

20. `test_no_io_imports` — mirror `test_package_seam.py::test_no_io_imports`: `import apollo.learner_model.personalization_select as mod; src = inspect.getsource(mod); for token in ("sqlalchemy","asyncpg","openai","neo4j"): assert token not in src`. Proves the PURE contract structurally. No deps.

21. `test_public_api_surface` — `from apollo.learner_model.personalization_select import (TEACHABLE_BAND_LO, TEACHABLE_BAND_HI, MASTERED_THRESHOLD, UNSEEN_MASTERY, REPROBE_CONFIDENCE, reference_entity_keys, prereqs_mastered, weak_teachable, coverage_score, personalize_selection)`; assert each callable is callable, each constant is the locked value, and `set(mod.__all__) == {those 10 names}`. Pins the seam WU-6A3 imports. No deps.

**Coverage note:** the module is fully pure ⇒ exhaustible to 100% line+branch. Tests 12-19 cover every branch of `personalize_selection` (raise / cold-start `is_empty` / `not weak` / scored-pick / tie); 4-9 cover `prereqs_mastered`/`weak_teachable` (no-edges/blocked/admitted/soft-hold/band-boundaries/empty); 3+11 cover `reference_entity_keys`/`coverage_score`. The private `_mastery_of_key` is exercised transitively by 5/6/8/16. diff-cover ≥95% vs `feat/apollo-kg-wu6a1-learner-profile-read` is comfortably met.

## Per-endpoint contracts

**JUSTIFIED ABSENT — no HTTP endpoint.** WU-6A2 adds no route, controller, or handler. It is a pure library function consumed in-process by WU-6A3's wiring (which itself adds NO new endpoint — it re-routes the EXISTING `/next` and session-init selection internally behind a flag, returning the unchanged response shape). There is therefore no method/path/auth/status surface to specify in this unit. The "contract" 6A2 owns is the function signature seam, pinned by test 21:

```
personalize_selection(
    profile: LearnerProfile,
    pool: Sequence[Problem],
    *,
    concept_id: int,
    difficulty: str,
    attempted_ids: Sequence[str],
) -> Problem
```
| Field | Value |
|-------|-------|
| Inputs | frozen `LearnerProfile` (6A1) + in-memory `Problem` pool + `concept_id:int` + `difficulty:str` + `attempted_ids` |
| Output | a `Problem` (the chosen candidate) |
| Raises | `PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)` when no in-difficulty unattempted candidate remains |
| Side effects | NONE (pure; no DB, no log, no mutation of inputs) |
| Idempotent | yes (deterministic: same inputs → same `Problem`, tie-break lowest `Problem.id`) |

## DI discovery findings

No constructor, no DI container, no provider registration — this is a Python module of free functions, matching the sibling pure module `belief.py` (which has zero `__init__`/class wiring). The only "injection" is by IMPORT:

- **`LearnerProfile`/`EntityProfile`** — imported from the SUBMODULE `apollo.learner_model.personalization_read`, NOT the package root (the root `__all__` is frozen by `test_package_seam.py`; 6A1 is not re-exported there). Singleton-free frozen dataclasses.
- **`_ENTRY_TYPE_TO_KIND_PREFIX`** — module-level frozen `dict` constant in `learner_model_seed.py` (a pure module with no `sqlalchemy` import — verified, so importing it does not leak IO into the pure unit). Imported by name; never mutated.
- **`PoolExhaustedError`** — class import from `apollo.errors`; constructor takes `(concept_cluster_id: str, difficulty: str)` (verified `errors.py:64`).
- **`Problem`/`ReferenceStep`** — pydantic models from `apollo.schemas.problem`.

Sibling conventions agree (belief/decay/negotiation are all free-function pure modules); no conflict to flag.

## Transaction scope decisions

**No transaction.** WU-6A2 performs ZERO writes and ZERO reads — it is a pure in-memory function. It touches no table, so there is no multi-table write to wrap, no compensation strategy needed, and no external service (no OpenAI/Whisper/Neo4j) called alongside any DB write. The transaction boundary for the eventual live path is owned entirely by WU-6A3's call-site (which only READS via 6A1 and never writes during selection). This unit cannot leave half-applied state because it applies no state.

## Error contract decisions

- **Exception class:** `PoolExhaustedError` (existing, `apollo/errors.py:61-70`, subclass of `ApolloError`). 6A2 introduces NO new exception class and NO new hierarchy — it RE-RAISES the exact exception `select_problem` already raises, with the byte-identical `concept_cluster_id=str(concept_id)` argument so the downstream 409 handler's back-compat message string is unchanged.
- **Global filter:** the FastAPI exception handlers registered in `apollo/api.py` catch `ApolloError` subclasses (per `errors.py:1-8` module docstring — "every raised exception surfaces as a visible error in the UI via the FastAPI exception handlers"). `PoolExhaustedError` → 409 to the frontend (the handler keys on `concept_cluster_id`/`difficulty`). 6A2 changes none of this; it preserves the existing response shape verbatim. **VERIFIED final string** (interpreter, 2026-06-19): `str(PoolExhaustedError("7","hard")) == "Problem pool exhausted for cluster '7' at difficulty 'hard'"` — pinned by test 17.
- 6A2 raises NOTHING else. Defensive cases (a `key` missing from `entity_id_by_key` in `prereqs_mastered`) resolve to a safe default (`True` / no prereqs), never an exception — there is no IO that could fail.

## Downstream consumers

- **WU-6A3 (next stacked unit)** is the ONLY consumer. It will `from apollo.learner_model.personalization_select import personalize_selection` (and `weak_teachable` for the observability-log's `n_weak_entities`) inside a new `select_problem_personalized` in `problem_selector.py`, wired into `session_init.py:58` and `next.py:79` behind `APOLLO_SESSION_PERSONALIZATION_ENABLED`. That is OUT OF SCOPE here; this unit only ships the importable seam.
- **Frontend / student-UI:** none in this unit. The `/next` and chat response SHAPES are unchanged even after 6A3 wires this in (only WHICH problem is returned changes, behind a default-OFF flag). No URL pattern, no payload key changes — nothing to grep in the student-UI for this unit.

## Risks

- **HIGH→RESOLVED — N+1 reference-entity hazard.** The reconstruction `reference_entity_keys` is PROVEN (3-way round-trip, interpreter-verified) to equal the seeded `canonical_key`s with zero per-problem IO. Mitigation: test 3 freezes the parity across all 5 seed files. Confidence: HIGH.
- **HIGH→RESOLVED — cold-start non-regression.** STEP 3's `is_empty or not weak → candidates[0]` is byte-identical to today's `select_problem` (same filter, same sorted-by-id order, same `candidates[0]`). Tests 14/15 pin it. Confidence: HIGH.
- **MEDIUM — tie-break must be order-independent.** Python's `max(candidates, key=...)` returns the FIRST max on ties; the pool ARRIVES sorted-by-id so first-max == lowest-id, but relying on arrival order is fragile. Mitigation: make lowest-`Problem.id` EXPLICIT in the key (test 13 runs the pool both forward and reversed and asserts identical). Confidence: HIGH once the explicit tie-break ships.
- **MEDIUM — fixture validity friction.** `Problem`'s validators (`order` 1..N contiguous, `uses_equations` real, `depends_on` resolves) make multi-`proc` constructed fixtures fiddly. Mitigation: the `_problem` helper documents the minimal valid shape; the discriminating tests (12-19) MAY load the REAL seed JSON via `Problem.model_validate(json.load(...))` to sidestep hand-authoring (the 4 intro key sets are exactly the seed's P1-P4). Confidence: HIGH.
- **LOW — `EntityProfile` import may be unused** (only `LearnerProfile` is dereferenced). Mitigation: keep it for the public seam OR drop it (Deviations). Either is fine; no behavior depends on it. Confidence: HIGH.
- **LOW — band/threshold values are hand-set calibration choices** (not spec-mandated). They are orchestrator-ADJUDICATED (split proposal §4); this unit does not re-open them. The golden vectors pin whatever is adjudicated. Confidence: HIGH (defensible per prior art: ZPD bands, greedy max-coverage).
- **LOW — slug-vs-int `concept_id`.** `concept_id` here is the INTEGER session id, used ONLY to build the byte-identical `PoolExhaustedError`; it never filters the pool (the pool is passed IN, already filed by directory/integer). Fixtures assert on `Problem.id`, NEVER `Problem.concept_id` (the divergent slug). Confidence: HIGH (verified P3/P4 carry `continuity_equation`/`volumetric_flow_rate` slugs yet belong to the bernoulli integer pool).

## Deviations I'd allow the executor

- **Tie-break form:** either explicit `min((p for p in candidates if score(p)==best), key=lambda p:p.id)` OR a single `max(candidates, key=lambda p:(score(p), <id-descending>))` — as long as the result is "highest coverage, ties → lowest `Problem.id`" and is pool-order-independent (test 13 enforces).
- **`coverage_score` memoization:** recompute per candidate (4-element pool — trivial) OR memoize into a `dict`. Either; keep it pure.
- **Dropping the unused `EntityProfile` import** if the linter flags it (no test depends on importing it from this module; tests import `EntityProfile` from `personalization_read` directly).
- **`weak_teachable` return type:** plain `dict[str, float]` (recommended, matches `belief.py` style) — do NOT introduce a dataclass for this internal intermediate.
- **Building the 4 discriminating-test problems** via `_problem` helper OR via `Problem.model_validate` on the real seed JSON. Prefer whichever is less error-prone; the real seed JSON is authoritative for the worked-example key sets.
- **Marker:** `@pytest.mark.unit` on each test (recommended) — acceptable to omit if the file has no other markers, but `--strict-markers` means any marker used MUST be `unit`/`integration`/`e2e`/`slow`/`llm`.

The executor MUST NOT: change the constants; re-export through `__init__.py`; add any IO; touch `problem_selector.py`/`next.py`/`session_init.py`/`personalization_read.py`/`learner_model_seed.py`/`belief.py`; use `networkx` or any new package; add skip/xfail.

## Out-of-scope boundaries (held firmly)

- **No live wiring** — `select_problem_personalized`, the flag module, the two call-site edits, and the observability log are ALL WU-6A3.
- **No persona conditioning** — `draft_reply`/`misconception_code` work is WU-6A4 (deferred).
- **No migration, no DB read/write, no Neo4j, no LLM, no container.** The REAL-INFRA verify gate must NOT trip; if it does the unit is mis-scoped. Only Gate 1 (pytest apollo) + Gate 3 (diff-cover) apply.
- **No difficulty auto-tune** — v1 clamps within the student's chosen difficulty (re-rank only within the in-difficulty candidate set).
- **No frozen-file edits** — `personalization_read.py`, `belief.py`, `problem_selector.py`, `learner_model_seed.py` are READ/IMPORTED only.
- **No package-root `__all__` change** — leaves `test_package_seam.py` green.
- **No `networkx`** — the prereq DAG walk is a stdlib list comprehension over `prereq_edges`.

## Verify commands

Use `ai-ta-backend/.venv/Scripts/python.exe`. Run from the `ai-ta-backend/` root.

1. Module imports clean (pure, no IO):
   `.venv/Scripts/python.exe -c "import apollo.learner_model.personalization_select as m; print(sorted(m.__all__))"`
2. Gate 1 — the new pure test file passes:
   `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_personalization_select.py -q`
3. Pre-existing seam test stays green (no `__init__.py`/seam regression):
   `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_package_seam.py -q`
4. No-IO structural check:
   `grep -nE "sqlalchemy|asyncpg|AsyncSession|select\(|neo4j|OpenAI|import os" apollo/learner_model/personalization_select.py` (expect no IO tokens; `import os` also absent)
5. Gate 3 — patch coverage ≥95% on changed lines vs the 6A1 parent:
   `.venv/Scripts/python.exe -m pytest apollo --cov=. --cov-report=xml -q` then
   `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu6a1-learner-profile-read --fail-under=95`
6. REAL-INFRA gate must NOT trip — confirm no new file under `tests/database/`, `apollo/knowledge_graph/`, `database/migrations/`, and `apollo/persistence/neo4j_client.py` untouched (the two new files are `apollo/learner_model/personalization_select.py` + `apollo/learner_model/tests/test_personalization_select.py` only).
