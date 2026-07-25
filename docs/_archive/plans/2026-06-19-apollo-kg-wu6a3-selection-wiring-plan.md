# WU-6A3 — selection wiring (live, real-PG) + observability — IMPLEMENTATION PLAN

**Date:** 2026-06-19
**Branch (already checked out):** `feat/apollo-kg-wu6a3-selection-wiring` — do NOT create/switch branches, do NOT push, do NOT open PRs.
**diff-cover compare branch:** `feat/apollo-kg-wu6a2-selection-algorithm` (patch coverage ≥95% measured against it).
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §1 L99-103 (v1 wedge: at session start read Layer 3 + select the problem best covering weak entities), §6 Readouts L470.
**Scoping authority:** `docs/superpowers/plans/2026-06-19-apollo-kg-wu6a-split-proposal.md` — WU-6A3 section (L265-294) + §4 decisions #1/#2/#9/#10 + ORCHESTRATOR ADJUDICATION.
**Status:** PLAN — code NOT written. This is the LAST unit of Phase 6 (the v1 wedge `6A1 → 6A2 → 6A3`). WU-6A4 (persona) is DEFERRED, out of scope here.

---

## 0. One-paragraph summary

WU-6A3 is the LIVE wiring that fires the session-personalization wedge at session start. It adds a tiny flag module (`personalization_flag.is_enabled()`), a new `select_problem_personalized(...)` orchestration function in `problem_selector.py` (reading the frozen WU-6A1 profile once + delegating scoring to the frozen WU-6A2 pure selector + emitting ONE structured observability log), and swaps BOTH `select_problem` call-sites (`session_init.py:58`, `next.py:79`) to route through it. `select_problem` itself is UNTOUCHED. With the flag OFF (default, incl. prod) the path is byte-identical to today; with the flag ON the prod cold-start path is STILL byte-identical (the learner-state table is empty because `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is OFF). NO migration, NO new package, NO Neo4j, NO LLM on this path.

---

## 1. Ground truth (re-verified against code, 2026-06-19)

All anchors below were read in this planning pass (file:line).

- **The selection seam (`apollo/overseer/problem_selector.py`).** `select_problem(db, *, concept_id, difficulty, attempted_ids)` (`:42-59`) does: `pool = await list_problems_for_concept(db, concept_id=concept_id)` (`:54`, ONE query, `sorted(problems, key=lambda p: p.id)` at `:39`); `candidates = [p for p in pool if p.difficulty == difficulty and p.id not in attempted]` (`:56`); `if not candidates: raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)` (`:57-58`); `return candidates[0]` (`:59`). The module has ZERO logging today. `list_problems_for_concept(db, *, concept_id)` (`:25-39`) is reusable as-is.

- **The TWO call-sites (both must be wired or the wedge never fires at session start).**
  - `apollo/hoot_bridge/session_init.py:58` — `init_session_from_hoot(*, db, user_id, search_space_id, hoot_transcript, difficulty)` (params `:39-46`); `concept_id` is the integer from `infer_concept_id(...)` (`:53`). The call at `:58-63` is `problem = await select_problem(db, concept_id=concept_id, difficulty=difficulty, attempted_ids=[])`. Return payload at `:96-107` (unchanged shape).
  - `apollo/handlers/next.py:79` — `handle_next(*, db, session_id, difficulty)`; `sess` loaded `with_for_update()` (`:37-41`); `attempted_ids` built at `:69-77`. The call at `:79-84` is `problem = await select_problem(db, concept_id=sess.concept_id, difficulty=difficulty, attempted_ids=attempted_ids)`. Return payload at `:98-109` (unchanged shape).

- **`ApolloSession` carries the three scoping keys (no extra query).** `apollo/persistence/models.py`: `user_id` UUID (`:205`), `search_space_id` Integer FK (`:206-211`), `concept_id` BigInteger FK nullable (`:217-222`), `current_problem_id` (`:225`). CONFIRMED: `sess.user_id` / `sess.search_space_id` / `sess.concept_id` are all in scope at `next.py:79`. `session_init.py:58` has `user_id`/`search_space_id` as params and `concept_id` as the inferred integer.

- **The flag idiom (mirror exactly).** `apollo/overseer/misconception.py:499-505`: `def is_enabled() -> bool: return os.getenv("APOLLO_MISCONCEPTION_ENABLED", "").lower() in {"1", "true", "yes", "on"}`. WU-6A3 mirrors this BYTE-FOR-BYTE for `APOLLO_SESSION_PERSONALIZATION_ENABLED` in a NEW `personalization_flag.py` so neither call-site duplicates the literal.

- **The logging template (mirror style).** `apollo/agent/apollo_llm.py:144-152`: `_LOG = logging.getLogger(__name__)` (`:30`); `_LOG.info("apollo_draft_reply", extra={"event": "llm_call", "purpose": ..., "model": ..., "tokens_in_estimated": ...})`. The house pattern is `_LOG.info(<message>, extra={"event": <string>, ...})`.

- **The frozen WU-6A1 read (import, do not modify).** `apollo/learner_model/personalization_read.py`: `async def read_learner_profile(db, *, user_id: str, search_space_id: int, concept_id: int) -> LearnerProfile`. `LearnerProfile` is a frozen dataclass with `by_canonical_key`, `prereq_edges`, `entity_id_by_key`, `key_by_entity_id`, `is_empty`. At most 3 scoped queries, none per-loop. On cold-start (no learner_state rows) `is_empty=True` and `by_canonical_key={}`.

- **The frozen WU-6A2 pure selection (import, do not modify).** `apollo/learner_model/personalization_select.py`: `personalize_selection(profile, pool, *, concept_id: int, difficulty: str, attempted_ids: Sequence[str]) -> Problem`. STEP 1 = the EXACT today filter; STEP 2 = byte-identical `PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)`; STEP 3 = `if profile.is_empty or not weak: return candidates[0]` (the non-regression branch); STEP 4 = deficit-weighted max-coverage, explicit lowest-`Problem.id` tie-break. Also exports `weak_teachable(profile) -> dict[str, float]` (used by 6A3 ONLY for the log's `n_weak_entities`).

- **The seed difficulty distribution (constrains the discriminating tests).** Verified all 5 `bernoulli_principle/problems/problem_*.json`: P1-P4 are `intro`, P5 (`bernoulli_full_find_p2`) is `standard`, NONE are `hard`. The pool is filed by DIRECTORY (the seeder assigns the bernoulli INTEGER `concept_id` to every JSON in the dir), so P3 (JSON slug `continuity_equation`) and P4 (JSON slug `volumetric_flow_rate`) ARE in the pool. **Every discriminating selection test pins `difficulty="intro"`; assert on `Problem.id`, NEVER filter by `Problem.concept_id` (the slug).**

- **Reconstructed `intro` entity-key sets (for the seeded-weak test).** Verified from the JSON `reference_solution` `(entry_type, id)` pairs via the frozen `_ENTRY_TYPE_TO_KIND_PREFIX` map:
  - P1 `bernoulli_horizontal_pipe_find_p2` → {eq.continuity, cond.incompressibility, eq.bernoulli, simp.horizontal_simplification, proc.plan_apply_continuity, proc.plan_apply_horizontal_simplification, proc.plan_solve_bernoulli_for_p2}
  - P2 `bernoulli_height_change_find_v2` → {eq.bernoulli, simp.equal_pressure_simplification, proc.plan_apply_equal_pressure_simplification, proc.plan_set_v1_zero_and_solve_bernoulli}
  - P3 `continuity_area_change_find_v2` → {cond.incompressibility, eq.continuity, proc.plan_invoke_incompressibility, proc.plan_solve_continuity_for_v2}
  - P4 `volumetric_flow_rate_find_Q` → {eq.flow_rate_definition, proc.plan_apply_flow_rate_definition}

- **Reusable real-PG harness (REUSE, do not reinvent).** `apollo/conftest.py` re-exports `_pg_url` + `db_session` from `tests/conftest.py`; `TEST_USER_ID` / `TEST_USER_ID_2` constants. `apollo/subjects/tests/_curriculum_fixtures.py` provides `load_bernoulli_problem_payloads()`, `seed_search_space`, `seed_concept`, `seed_problems`, `seed_course`. `tests/database/test_personalization_read_postgres.py` (WU-6A1) provides `_seed_entity` / `_seed_prereq` / `_seed_state` patterns. `tests/database/test_done_layer3_route_postgres.py` (WU-5A2) provides the `ApolloSession`/`ProblemAttempt` session-seed pattern. `apollo/overseer/tests/test_problem_selector.py` is the existing selector real-PG test.

- **NO migration.** All reads hit existing migration-026 tables. `next_free_migration=030` stays unused.

- **Owner doc** `docs/architecture/apollo.md` already declares `owns: [apollo/**, ...]`, `last_verified: 2026-06-19`. The learner_model row (line 41) already documents `personalization_read.py`/`personalization_select.py`. The overseer row (line 42) lists `problem_selector.py` but NOT `personalization_flag.py`/`select_problem_personalized` — that is the drift to reconcile.

## 2. Files to create / edit (exact)

**The ONLY files this unit may touch** (new files belong in the same packages):

| Action | File | What changes |
|---|---|---|
| CREATE | `apollo/overseer/personalization_flag.py` | NEW tiny module — `is_enabled()` mirroring `misconception.py:499-505`. Owns the literal. |
| EDIT | `apollo/overseer/problem_selector.py` | ADD `async def select_problem_personalized(...)` + a module logger `_LOG`. `select_problem` + `list_problems_for_concept` UNTOUCHED. |
| EDIT | `apollo/handlers/next.py` | Swap the `select_problem(...)` call at `:79` → `select_problem_personalized(...)`; update the import. ~6 lines. |
| EDIT | `apollo/hoot_bridge/session_init.py` | Swap the `select_problem(...)` call at `:58` → `select_problem_personalized(...)`; update the import. ~6 lines. |
| CREATE | `tests/database/test_personalization_select_route_postgres.py` | NEW real-PG route test file (Testcontainers `db_session`). |
| EDIT | `docs/architecture/apollo.md` | Drift reconcile: overseer module-map row (add `personalization_flag.py` + `select_problem_personalized`), note both call-sites route through it, bump `last_verified` → 2026-06-19. |

**FROZEN — DO NOT MODIFY** (import only): `apollo/learner_model/personalization_read.py` (WU-6A1), `apollo/learner_model/personalization_select.py` (WU-6A2), `apollo/overseer/problem_selector.py::select_problem` (other callers depend on its signature/behavior), `apollo/overseer/misconception.py`.

**NO migration file. NO new package (`networkx` forbidden). NO Neo4j/LLM on this path.**

## 3. Public signatures (backward-compat preserved)

### 3.1 `apollo/overseer/personalization_flag.py` (NEW)

```python
"""Master gate for the v1 session-personalization wedge (WU-6A3).

Mirrors apollo/overseer/misconception.py:is_enabled exactly so the two
selection call-sites (session_init.py, next.py) and select_problem_personalized
never duplicate the env-var literal. Default OFF everywhere incl. prod.

Orthogonal to APOLLO_GRAPH_SIM_LAYER3_ENABLED (which gates the WRITE / table
population): personalization can be flag-ON in prod and still be a total no-op
because apollo_learner_state is empty until LAYER3 is flipped (double-gated).
"""

from __future__ import annotations

import os

__all__ = ["is_enabled"]


def is_enabled() -> bool:
    """True iff APOLLO_SESSION_PERSONALIZATION_ENABLED is truthy."""
    return os.getenv("APOLLO_SESSION_PERSONALIZATION_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
```

### 3.2 `apollo/overseer/problem_selector.py` — ADD (signature LOCKED)

```python
async def select_problem_personalized(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    attempted_ids: Sequence[str],
) -> Problem: ...
```

- **`select_problem` is UNCHANGED** (signature + body byte-identical; read-back/other callers depend on it).
- `select_problem_personalized` is keyword-only after `db` (matches `select_problem`'s kwarg-only style) and adds `user_id` + `search_space_id` as the only new params.
- Imports added at module top: `import logging`; `from apollo.learner_model.personalization_read import read_learner_profile`; `from apollo.learner_model.personalization_select import personalize_selection, weak_teachable`; `from apollo.overseer import personalization_flag`. Module logger: `_LOG = logging.getLogger(__name__)`.

> **Import-cycle check (must hold):** `problem_selector` (overseer) → imports `personalization_read` + `personalization_select` (learner_model). Neither learner_model module imports anything from `overseer`, so no cycle. `personalization_flag` imports only `os`. Confirmed safe.

### 3.3 Call-site signatures — UNCHANGED

`init_session_from_hoot(...)` and `handle_next(...)` keep their existing signatures AND return-payload shapes (`session_init.py:96-107`, `next.py:98-109`). Only WHICH `Problem` populates the payload changes when the flag is ON. **No student-UI change.**

## 4. Behavior contract (flag-OFF / flag-ON-empty / flag-ON-weak)

The body of `select_problem_personalized` (the canonical shape the executor implements):

```python
async def select_problem_personalized(
    db, *, user_id, search_space_id, concept_id, difficulty, attempted_ids
) -> Problem:
    # FLAG-OFF: the EXACT old path — no profile read, NO log, byte-identical.
    if not personalization_flag.is_enabled():
        return await select_problem(
            db, concept_id=concept_id, difficulty=difficulty, attempted_ids=attempted_ids
        )

    # FLAG-ON: read the pool once + the profile once at the SELECTION seam
    # (not per-turn, not in a candidate loop -> no N+1).
    pool = await list_problems_for_concept(db, concept_id=concept_id)
    profile = await read_learner_profile(
        db, user_id=user_id, search_space_id=search_space_id, concept_id=concept_id
    )
    chosen = personalize_selection(
        profile, pool, concept_id=concept_id, difficulty=difficulty, attempted_ids=attempted_ids
    )

    # ONE structured observability log (the only runtime signal the wedge fired
    # vs degraded). See §5. NOTE: weak_teachable() is recomputed here PURELY for
    # the log's n_weak_entities + fallback_fired fields; it is the same pure
    # function personalize_selection used internally (cheap, no IO).
    weak = weak_teachable(profile)
    _LOG.info(
        "apollo_select_problem_personalized",
        extra={
            "event": "personalized_selection",
            "personalization_enabled": True,
            "profile_is_empty": profile.is_empty,
            "n_weak_entities": len(weak),
            "chosen_problem_id": chosen.id,
            "fallback_fired": profile.is_empty or not weak,
        },
    )
    return chosen
```

### Three behavioral states (each pinned by a test):

1. **FLAG-OFF (default, incl. prod).** Byte-identical to today: delegates to the untouched `select_problem`. NO `read_learner_profile` call, NO `list_problems_for_concept` extra call, NO log. The selected `Problem.id` is exactly today's `candidates[0]`. This is the LOAD-BEARING non-regression anchor — if any existing `next.py`/`session_init.py` route test breaks with the flag OFF, the wiring changed behavior (a bug).

2. **FLAG-ON, empty `apollo_learner_state` (THE PROD PATH).** Even with the flag ON, `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is OFF in prod → the table is empty → `read_learner_profile` returns `is_empty=True` → `personalize_selection` hits STEP 3 (`if profile.is_empty or not weak: return candidates[0]`) → identical `Problem.id` to flag-OFF. The log fires with `profile_is_empty=True, n_weak_entities=0, fallback_fired=True`.

3. **FLAG-ON, seeded weak profile (the wedge actually fires).** With learner-state rows making `eq.continuity`/`cond.incompressibility` weak-and-teachable (prereqs mastered) at `difficulty="intro"`, `personalize_selection` returns the higher-coverage problem (Example A → P1 `bernoulli_horizontal_pipe_find_p2`), distinct from `candidates[0]` when they differ. The log fires with `profile_is_empty=False, n_weak_entities≥1, fallback_fired=False`.

**`PoolExhaustedError` is raised byte-identically flag-ON** (e.g. `difficulty="hard"`, 0 candidates): `personalize_selection` raises `PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)` before any log — same as today's `select_problem`.

## 5. The observability log (exact shape)

Exactly ONE `_LOG.info` per personalized (flag-ON) selection; flag-OFF emits NONE. Mirrors the `apollo_llm.py:144-152` house style (`extra={"event": ..., ...}`).

| Field | Type | Meaning |
|---|---|---|
| (message arg) | `"apollo_select_problem_personalized"` | log record message |
| `event` | `"personalized_selection"` | the structured event tag (the field tests filter on) |
| `personalization_enabled` | `True` | always True here (flag-OFF never reaches the log) |
| `profile_is_empty` | `bool` | `profile.is_empty` — True on the prod cold-start path |
| `n_weak_entities` | `int` | `len(weak_teachable(profile))` |
| `chosen_problem_id` | `str` | `chosen.id` (the selected `Problem.id`) |
| `fallback_fired` | `bool` | `profile.is_empty or not weak` — True when the selection degraded to `candidates[0]` |

`top_coverage_score` is OPTIONAL and is NOT included in v1 (keeps the field set minimal; can be added later without contract change).

**Why these fields:** they are the only way an operator can tell, after `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is eventually flipped, whether a given selection actually personalized (`fallback_fired=False`, `n_weak_entities>0`) or silently degraded to today's behavior (`fallback_fired=True`). Required before any prod flag flip.

## 6. TDD-ordered implementation steps

REAL tests FIRST. No skip-marks, no `xfail`, no assert-nothing. Interpreter is `ai-ta-backend/.venv/Scripts/python.exe` (a bare-`python` ImportError is an interpreter-selection error, not a blocker). The real-PG `tests/database` gate WILL trigger (new file under `tests/database/`) and MUST run GREEN-not-skipped with Docker up (pgvector:pg16).

1. **RED — write `tests/database/test_personalization_select_route_postgres.py`** with the full §7 test list. They fail because `select_problem_personalized` and `personalization_flag` do not exist yet (ImportError / AttributeError). Reuse the seed helpers from `_curriculum_fixtures.py` + the WU-6A1 `_seed_entity`/`_seed_prereq`/`_seed_state` patterns + the WU-5A2 session-seed pattern. Mock NOTHING DB-side (real PG); env flag set/unset via `monkeypatch.setenv` / `monkeypatch.delenv`; no LLM/network on this path so nothing else to mock.
2. **GREEN-step-A — create `apollo/overseer/personalization_flag.py`** (§3.1). Run the flag-level tests → green.
3. **GREEN-step-B — add `select_problem_personalized` + `_LOG` to `apollo/overseer/problem_selector.py`** (§3.2/§4). Leave `select_problem`/`list_problems_for_concept` byte-identical. Run the unit + route tests that drive the function directly → green.
4. **GREEN-step-C — swap the call-site in `apollo/handlers/next.py`** (`:79` → `select_problem_personalized(db, user_id=sess.user_id, search_space_id=sess.search_space_id, concept_id=sess.concept_id, difficulty=difficulty, attempted_ids=attempted_ids)`; change `from apollo.overseer.problem_selector import select_problem` → `... import select_problem_personalized`). Run the `next.py` route tests (flag-OFF byte-identical + flag-ON) → green.
5. **GREEN-step-D — swap the call-site in `apollo/hoot_bridge/session_init.py`** (`:58` → `select_problem_personalized(db, user_id=user_id, search_space_id=search_space_id, concept_id=concept_id, difficulty=difficulty, attempted_ids=[])`; change the import). Run the `session_init` route tests → green.
6. **REGRESSION — run the EXISTING apollo route/selector suites unchanged** (`apollo/overseer/tests/test_problem_selector.py`, `apollo/handlers/tests/test_next.py` + `test_next_db.py`, any `session_init` test). With the flag default-OFF these MUST stay green untouched — that is the live non-regression proof.
7. **REFACTOR/LINT** — `black` + `ruff` + `isort` on the touched files; confirm no `print()` (use `_LOG`).
8. **COVERAGE** — `diff-cover` ≥95% vs `feat/apollo-kg-wu6a2-selection-algorithm` (§9).
9. **DRIFT** — update `docs/architecture/apollo.md` (§8) in the SAME commit.

### Verification commands (executor runs these locally — Docker UP)
- [ ] `ai-ta-backend/.venv/Scripts/python.exe -m pytest tests/database/test_personalization_select_route_postgres.py -v` — expect: all GREEN, NONE skipped (a skip = gate FAIL).
- [ ] `ai-ta-backend/.venv/Scripts/python.exe -m pytest apollo/overseer/tests/test_problem_selector.py apollo/handlers/tests -v` — expect: existing suite still GREEN (flag-OFF non-regression).
- [ ] `ai-ta-backend/.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml -q` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu6a2-selection-algorithm --fail-under=95` — expect: ≥95% on changed lines.
- [ ] `ruff check apollo/overseer/personalization_flag.py apollo/overseer/problem_selector.py apollo/handlers/next.py apollo/hoot_bridge/session_init.py tests/database/test_personalization_select_route_postgres.py` — expect: no new warnings.

## 7. Full test list (names + assertions + how deps are mocked)

All tests live in `tests/database/test_personalization_select_route_postgres.py` with `pytestmark = pytest.mark.integration` and run on the real `db_session` fixture (Testcontainers pgvector:pg16, savepoint rollback per test). **Deps mocked:** ONLY the env flag (`monkeypatch.setenv`/`monkeypatch.delenv` on `APOLLO_SESSION_PERSONALIZATION_ENABLED`). NO DB mock (real PG), NO LLM/network on this path (selection is pure-PG + pure-Python). The log is captured with the `caplog` fixture (`caplog.set_level(logging.INFO, logger="apollo.overseer.problem_selector")`).

### Shared local seed helpers (define at top of the file, adapted from the cited harnesses)
- `_seed_pool(db, *, course_slug)` → `(sid, cid, codes)`: calls `seed_course(db, subject_slug=..., concept_slug="bernoulli_principle", problems=load_bernoulli_problem_payloads())` so all 5 bernoulli problems share the INTEGER `cid` (directory-filing). Returns the integer `concept_id` — tests assert on `Problem.id`, never `Problem.concept_id`.
- `_seed_entity(db, *, concept_id, canonical_key, kind="equation")` → `entity_id` (from the WU-6A1 read-test pattern).
- `_seed_prereq(db, *, from_id, to_id)` (WU-6A1 pattern).
- `_seed_state(db, *, sid, entity_id, mastery, confidence, misconception_code=None, user_id=TEST_USER_ID)` (WU-6A1 pattern — supplies a length-3 `belief` to satisfy NOT NULL).
- `_seed_session(db, *, sid, cid, current_problem_id, phase, user_id=TEST_USER_ID)` → `ApolloSession` (WU-5A2 pattern) for the `handle_next` route tests.

### A. Flag module (unit — no DB)
1. **`test_flag_is_enabled_truthy_set`** — with `monkeypatch.setenv("APOLLO_SESSION_PERSONALIZATION_ENABLED", v)` for `v` in `{"1","true","TRUE","yes","on","On"}`, `personalization_flag.is_enabled()` is `True`. Asserts the case-insensitive truthy set mirrors `misconception.is_enabled`.
2. **`test_flag_is_disabled_when_unset_or_falsey`** — unset (`monkeypatch.delenv(..., raising=False)`) AND values in `{"","0","false","no","off","nope"}` → `is_enabled()` is `False`. (Covers the default-OFF prod posture.)

### B. `select_problem_personalized` unit-level (driven directly, real PG)
3. **`test_flag_off_byte_identical_to_select_problem`** — flag UNSET. Seed the bernoulli pool. Call BOTH `select_problem(db, concept_id=cid, difficulty="intro", attempted_ids=[])` and `select_problem_personalized(db, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid, difficulty="intro", attempted_ids=[])`. Assert SAME `Problem.id`. This is the load-bearing non-regression guard at the unit seam.
4. **`test_flag_off_emits_no_personalized_log`** — flag UNSET, with `caplog` at INFO. Call `select_problem_personalized(...)`. Assert NO record has `getattr(rec, "event", None) == "personalized_selection"`. (Flag-OFF is silent.)
5. **`test_flag_on_empty_state_returns_candidates_zero`** — flag ON, pool seeded, ZERO `apollo_learner_state` rows (entities may or may not be seeded; `is_empty=True` either way). Assert `select_problem_personalized(...).id == select_problem(...).id` at `difficulty="intro"` (the prod cold-start path, byte-identical fallback).
6. **`test_flag_on_empty_state_log_marks_fallback`** — same setup as #5 with `caplog`. Assert exactly ONE `event="personalized_selection"` record with `profile_is_empty is True`, `n_weak_entities == 0`, `fallback_fired is True`, and `chosen_problem_id == candidates[0].id`.
7. **`test_flag_on_seeded_weak_selects_high_coverage_problem`** (the wedge fires) — flag ON, `difficulty="intro"`. Seed the bernoulli pool; seed `apollo_kg_entities` rows whose `canonical_key`s match the reconstructed keys (`eq.continuity`, `cond.incompressibility`, `eq.bernoulli`) under `cid`; seed `apollo_learner_state`: `eq.continuity` mastery 0.35 / conf 0.6, `cond.incompressibility` mastery 0.40 / conf 0.6, `eq.bernoulli` mastery 0.68 / conf 0.6 (all in-band, no prereq edges → prereqs trivially mastered). Assert `select_problem_personalized(...).id == "bernoulli_horizontal_pipe_find_p2"` (P1, the max-coverage problem — Example A), and assert it differs from… nothing required (P1 also happens to be `candidates[0]` here); to make the discrimination NON-vacuous, additionally seed `attempted_ids=["bernoulli_horizontal_pipe_find_p2"]` in a sibling assertion and confirm the next pick is the higher-coverage of the remaining `intro` candidates (P3 `continuity_area_change_find_v2`, covering {eq.continuity, cond.incompressibility} = deficit 0.65+0.60) over P2 (covers {eq.bernoulli} = 0.32) — `chosen.id == "continuity_area_change_find_v2"`. This proves coverage scoring, not first-in-list.
8. **`test_flag_on_seeded_weak_log_marks_personalized`** — same seed as #7 (the non-attempted P1 case), with `caplog`. Assert ONE `event="personalized_selection"` record with `profile_is_empty is False`, `n_weak_entities == 3`, `fallback_fired is False`, `chosen_problem_id == "bernoulli_horizontal_pipe_find_p2"`.
9. **`test_flag_on_prereq_blocked_weak_excluded_falls_back`** — flag ON, `difficulty="intro"`. Seed `eq.continuity` in-band (0.40) with a prereq edge `eq.continuity → cond.incompressibility`, and seed `cond.incompressibility` with NO state row (unseen → 0.50 < 0.70 → blocks). With `eq.continuity` the only in-band entity, `weak == {}` → fallback to `candidates[0]`. Assert `chosen.id == candidates[0].id` and (via `caplog`) `fallback_fired is True`, `n_weak_entities == 0`. (Pins that the wiring threads 6A1's prereq edges into 6A2's gate.)
10. **`test_flag_on_pool_exhausted_hard_raises_byte_identical`** — flag ON, `difficulty="hard"` (0 hard candidates in the seed). Assert `pytest.raises(PoolExhaustedError)` with `exc.difficulty == "hard"` and `exc.concept_cluster_id == str(cid)` — byte-identical to today. Assert NO `personalized_selection` log fired (the raise precedes the log).
11. **`test_flag_on_pool_exhausted_flag_off_parity`** — same `difficulty="hard"`: assert `select_problem` ALSO raises with identical `.difficulty`/`.concept_cluster_id`, proving flag-ON exhaustion == flag-OFF exhaustion.

### C. Course-isolation end-to-end (the §1.4 invariant through the wiring)
12. **`test_course_isolation_personalized_selection`** — flag ON. Seed course A (`sid_a`, `cid_a`) with the bernoulli pool + weak `eq.continuity`/`cond.incompressibility` learner_state for `TEST_USER_ID`. Seed course B (`sid_b`, `cid_b`) with its OWN bernoulli pool but DIFFERENT mastery (e.g. everything > 0.7 so no weak entities). Call `select_problem_personalized(db, user_id=TEST_USER_ID, search_space_id=sid_b, concept_id=cid_b, difficulty="intro", attempted_ids=[])`. Assert the course-B selection used course B's (empty-weak) profile → `candidates[0]` for course B, and that course A's weak mastery never influenced it (`fallback_fired is True` for the course-B call via `caplog`). Conversely the course-A call personalizes. Proves mastery never crosses courses through the live seam.

### D. Route integration — `handle_next` (real PG)
13. **`test_handle_next_flag_off_byte_identical`** — flag UNSET. Seed pool + an `ApolloSession` (`phase=REPORT`, `current_problem_id` set, `concept_id=cid`) + a prior `ProblemAttempt`. Call `handle_next(db=db_session, session_id=sess.id, difficulty="intro")`. Assert the returned `payload["problem"]["id"]` equals what `select_problem(db, concept_id=cid, difficulty="intro", attempted_ids=<attempted>)` returns for the same state. Assert the payload SHAPE is unchanged (keys: `session_id`, `attempt_id`, `problem{id,concept_id,difficulty,problem_text,given_values,target_unknown}`). The live non-regression anchor at the route.
14. **`test_handle_next_flag_on_seeded_weak_personalizes`** — flag ON. Same session seed, plus weak learner_state (Example-A entities) under the session's `user_id`/`search_space_id`/`concept_id`, with `current_problem_id`/prior attempt = P? chosen so the remaining `intro` candidates discriminate (e.g. mark P1 attempted so the personalized pick is P3 over P2). Assert `payload["problem"]["id"] == "continuity_area_change_find_v2"`. Confirms `handle_next` threads `sess.user_id`/`sess.search_space_id`/`sess.concept_id` correctly. Assert ONE `personalized_selection` log fired.
15. **`test_handle_next_flag_on_empty_state_matches_flag_off`** — flag ON, no learner_state. Assert `handle_next` returns the SAME `problem.id` as the flag-OFF run for an identical session (prod cold-start parity at the route).

### E. Route integration — `init_session_from_hoot` (real PG, the LITERAL session start)
16. **`test_init_session_flag_off_byte_identical`** — flag UNSET. Seed the bernoulli pool + the course's `apollo_concepts` candidate. **Mock the concept-inference LLM hop exactly as the existing `test_session_init_db.py` does:** `with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):` (deterministic, NO live OpenAI call — this is the only LLM on the init path, and it is upstream of the selection seam). Call `init_session_from_hoot(db=db_session, user_id=TEST_USER_ID, search_space_id=sid, hoot_transcript="...", difficulty="intro")`. Assert `payload["problem"]["id"]` equals `select_problem(db, concept_id=cid, difficulty="intro", attempted_ids=[]).id` and the payload SHAPE is unchanged.
17. **`test_init_session_flag_on_seeded_weak_personalizes`** — flag ON, same `patch(...infer_concept_id, return_value=cid)`. Seed weak Example-A learner_state for `(TEST_USER_ID, sid, cid)`. Assert `payload["problem"]["id"] == "bernoulli_horizontal_pipe_find_p2"` (P1, `attempted_ids=[]` so the max-coverage problem). Assert ONE `personalized_selection` log fired with `chosen_problem_id == "bernoulli_horizontal_pipe_find_p2"`. This proves the wedge fires at the LITERAL session start (`session_init.py:58`), not only at `/next`.
18. **`test_init_session_flag_on_empty_state_matches_flag_off`** — flag ON, same `patch(...)`, no learner_state → `init_session_from_hoot` picks the same `problem.id` as flag-OFF (cold-start parity at the init seam).

> **Mocking note for tests 13-18:** `handle_next` does NOT call `infer_concept_id` (it reads `sess.concept_id`), so its route tests need NO LLM mock. `init_session_from_hoot` DOES call `infer_concept_id` upstream of selection; patch it (per the established harness) so no live LLM fires. The selection seam itself (`select_problem_personalized` → `read_learner_profile` → `personalize_selection`) is pure-PG + pure-Python — no mock needed there.

> **Discrimination caveat (LOCKED by §1 seed distribution):** every test that asserts a personalized pick DIFFERS from `candidates[0]` uses `difficulty="intro"` (the only multi-candidate difficulty) and arranges `attempted_ids` so ≥2 `intro` candidates remain with distinct coverage. Never assert "differs" at `standard` (one candidate → vacuous) or `hard` (raises).

### Coverage of the swapped call-sites
The `next.py:79` and `session_init.py:58` changed lines are exercised by tests 13-18 (real route calls), so `diff-cover` sees them executed under BOTH flag states. `select_problem_personalized`'s every branch (flag-off delegate / flag-on read+score+log / weak vs empty / log fields) is covered by tests 3-12. `personalization_flag.is_enabled` by tests 1-2.

## 8. Owner-doc (drift) updates — same commit

Owner doc: `docs/architecture/apollo.md` (already `owns: apollo/**` — no `owns:` change). Reconcile in the SAME commit as the code:

1. **Overseer module-map row (line 42).** Add `personalization_flag.py` to the file list and extend the description: append a clause documenting `personalization_flag` (`is_enabled()` mirrors `misconception.is_enabled`; gates `APOLLO_SESSION_PERSONALIZATION_ENABLED`, default OFF) and `problem_selector.select_problem_personalized` (flag-gated path that reads the WU-6A1 `LearnerProfile` once + delegates to the WU-6A2 `personalize_selection`, emits ONE `event=personalized_selection` structured log; flag-OFF/empty-state falls back byte-identically to `select_problem`, which is UNCHANGED). Note the double-gating: flag ON is still a no-op in prod until `APOLLO_GRAPH_SIM_LAYER3_ENABLED` populates `apollo_learner_state`.

2. **Hoot-bridge row (line 47).** Append: "the DB problem selection now routes through `overseer.select_problem_personalized` (WU-6A3) — flag-gated; flag-OFF is byte-identical to `select_problem`."

3. **Handlers row (line 33).** Add a clause to the `next.py` description: "`next.py` problem advance routes through `overseer.select_problem_personalized` (WU-6A3, flag-gated)."

4. **Env-flags bullet (line 135 area).** Add `APOLLO_SESSION_PERSONALIZATION_ENABLED` (default OFF everywhere incl. prod; the v1 session-personalization wedge gate, owned by `overseer/personalization_flag.is_enabled()`; orthogonal to `APOLLO_GRAPH_SIM_LAYER3_ENABLED` which populates the read table — both must be ON for the wedge to do anything).

5. **Known downstream item (note, in a conventions/notes location).** Behind the flag the same student gets a personalized problem ORDER with no UI affordance explaining why; the response shapes are unchanged so there is no student-UI change required. A future "why this problem" surfacing is an additive, flag-gated student-ui follow-up (out of scope here).

6. **Bump `last_verified` → `2026-06-19`** (line 13). NOTE: the doc already reads `2026-06-19` and the WU-6A3 task brief's binding line also says "2026-06-16" in one place; today is 2026-06-19 and the recon facts + split proposal use 2026-06-19, so use **2026-06-19** (the value is idempotent if already set). Flagged as a LOW risk in §10.

> The learner_model row (line 41) ALREADY documents `personalization_read.py`/`personalization_select.py` (landed by WU-6A1/6A2) — no change needed there.

## 9. Coverage strategy (≥95% patch, diff-cover)

- **Compare branch:** `feat/apollo-kg-wu6a2-selection-algorithm`. Command: `pytest --cov=. --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu6a2-selection-algorithm --fail-under=95`.
- **Why `--cov=.` measures everything:** all four changed source files live under `apollo/` (`apollo/overseer/personalization_flag.py`, `apollo/overseer/problem_selector.py`, `apollo/handlers/next.py`, `apollo/hoot_bridge/session_init.py`) and `--cov=.` covers the whole tree, so every changed line is measured — including the route handlers.
- **Changed-line inventory and which test covers it:**
  - `personalization_flag.py` `is_enabled` body → tests 1-2.
  - `problem_selector.py` new imports + `_LOG` + `select_problem_personalized` (flag-off branch / flag-on read+score / log emission / `weak`/`fallback_fired` computation) → tests 3-12.
  - `next.py` swapped call + import → tests 13-15 (executed under flag-OFF and flag-ON).
  - `session_init.py` swapped call + import → tests 16-18.
- **The real-PG gate is GREEN-not-skipped:** the new `tests/database/` file forces the Testcontainers gate; a skip (Docker down) is a FAIL. Run with `ai-ta-backend/.venv/Scripts/python.exe`.
- **No exemptions anticipated.** Every changed line is reachable by a real test. If `infer_concept_id` proves awkward to drive deterministically in tests 16-18, prefer seeding a single candidate concept (deterministic resolution) over exempting — "hard to test" is not an exemption (CLAUDE.md).

## 10. Risks (confidence-rated)

1. **HIGHEST — both call-sites must be wired or the wedge never fires at session start.** `session_init.py:58` is the LITERAL session start; `next.py:79` is subsequent advances. Wiring only one misses half the wedge. Mitigation: this plan wires BOTH; tests 16-18 prove init personalizes, tests 13-15 prove `/next` personalizes. Confidence: HIGH (both sites + their in-scope scoping keys verified).
2. **HIGH→MITIGATED — flag-OFF must be byte-identical (the live non-regression anchor).** Any behavior change with the flag OFF on the live Apollo session is a bug. Mitigation: `select_problem_personalized` flag-OFF branch delegates to the UNTOUCHED `select_problem` (no read, no log); tests 3, 4, 13, 16 pin byte-identity at unit + both routes; step 6 re-runs the existing suite unchanged. Confidence: HIGH.
3. **HIGH→MITIGATED — prod cold-start parity with the flag ON.** `APOLLO_GRAPH_SIM_LAYER3_ENABLED` OFF ⇒ empty `apollo_learner_state` ⇒ `is_empty=True` ⇒ `candidates[0]`. Mitigation: 6A2's STEP-3 branch + tests 5, 6, 15, 18. Confidence: HIGH (the double-gating is by design).
4. **MEDIUM — `sess.concept_id` is nullable (`models.py:217`).** If a session row ever has `concept_id IS NULL`, `select_problem_personalized` passes `None` to `read_learner_profile`/`list_problems_for_concept`. This is NO WORSE than today: `select_problem` already receives `sess.concept_id` and would behave identically (an empty pool → `PoolExhaustedError`). The wiring does not introduce a new null hazard. Mitigation: do NOT add new null handling (out of scope, would diverge from `select_problem`); a test is optional. Confidence: MEDIUM that this never happens in practice (sessions are created with a resolved `concept_id`), HIGH that the wiring doesn't make it worse.
5. **MEDIUM — discriminating tests are constrained to `difficulty="intro"`.** The seed has 4 intro / 1 standard / 0 hard, so a "personalized ≠ candidates[0]" assertion is only meaningful at `intro`. Mitigation: every such test pins `intro` and arranges `attempted_ids`; `standard`/`hard` are used only for degeneracy/raise assertions. Confidence: HIGH (seed verified).
6. **LOW — `infer_concept_id` is a live LLM call on the init path.** It is UPSTREAM of the selection seam and is mocked via the established `patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid)`. No live LLM fires in any test. Confidence: HIGH.
7. **LOW — the real-PG gate skips if Docker is down.** A skip is a gate FAIL. Mitigation: run with Docker up and `ai-ta-backend/.venv/Scripts/python.exe`; a bare-`python` ImportError is interpreter selection, not a real failure. Confidence: HIGH.
8. **LOW — `last_verified` date ambiguity.** The brief mentions both 2026-06-16 and 2026-06-19; the doc + recon + proposal use 2026-06-19, which is today. Use **2026-06-19**. Confidence: HIGH (idempotent — doc already reads it).
9. **LOW — `weak_teachable` is computed twice on the flag-ON path** (once inside `personalize_selection`, once in 6A3 for the log). It is a pure, in-memory, sub-millisecond function over a tiny profile; the duplication buys a clean log without threading internal state out of the frozen 6A2 function. Acceptable. Confidence: HIGH.

## 11. Out-of-scope boundaries (held firmly)

- **WU-6A4 persona conditioning** (`draft_reply` kwarg, `chat.py` per-turn read, leakage corpus). DEFERRED. Do NOT touch `apollo/agent/apollo_llm.py` or `apollo/handlers/chat.py`.
- **Modifying `select_problem`** — it is FROZEN (other callers depend on it). 6A3 only ADDS `select_problem_personalized` alongside.
- **Modifying the frozen WU-6A1/6A2 modules** (`personalization_read.py`, `personalization_select.py`). Import only.
- **Any migration** (`next_free_migration=030` stays unused). No `ApolloSession.misconception_code` cache column (that is a deferred 6A4 concern).
- **New packages** (`networkx` explicitly forbidden), Neo4j reads, or LLM calls on the selection path.
- **Student-UI changes.** Response shapes are unchanged; only the selected problem content changes behind the flag. The "why this problem" surfacing is a recorded follow-up, not built here.
- **Difficulty auto-tune.** v1 clamps within the student's chosen difficulty (the existing `p.difficulty == difficulty` filter); no override.
- **Branch/PR operations.** Work only on the checked-out `feat/apollo-kg-wu6a3-selection-wiring`; no branch create/switch, no push, no PR.
- **Remote DB.** All tests run on LOCAL Testcontainers pgvector:pg16. Never apply anything to any remote Supabase project.

## 12. Deviations the executor MAY make

The executor MAY, without re-consulting the planner:

- Adjust exact test FUNCTION NAMES, docstrings, and helper-fixture factoring (e.g. move `_seed_pool` into a module-level fixture) as long as the §7 assertions are all preserved and no test is weakened/skipped.
- Choose the precise weak-profile mastery values in tests 7/14/17 as long as they keep `eq.continuity`/`cond.incompressibility`/`eq.bernoulli` strictly inside `[0.3, 0.7]` and produce the asserted coverage ordering (P1 max, or P3 > P2 when P1 is attempted).
- Reorder imports / apply `black`/`isort`/`ruff` autofixes on touched files.
- Add `top_coverage_score` to the log IF it is trivial — but it is NOT required and its absence is correct for v1.
- Pick the exact `hoot_transcript` string and candidate-concept seed for the init tests (the LLM hop is mocked, so the transcript content is immaterial to resolution).
- Add an OPTIONAL extra test for the `sess.concept_id IS NULL` path (risk #4) if cheap — not required.

The executor MUST NOT, without escalating:

- Change the public signature of `select_problem_personalized` (§3.2) or touch `select_problem`'s signature/body.
- Weaken or skip any of tests 3, 4, 13, 16 (the flag-OFF byte-identical anchors) — these are the load-bearing live-regression guards.
- Add a runtime dependency, a migration, a Neo4j read, or an LLM call on the selection path.
- Drop the observability log or change its `event="personalized_selection"` tag (downstream ops/alerting will key on it).
- Mark the real-PG test file `skip`/`xfail` to dodge a Docker-down environment — the gate must run GREEN.

---

## Deploy handoff (HUMAN/CI only — never executed by feller)

There is NO migration and NO remote DB action in this unit. At deploy time the only operational lever is the env flag `APOLLO_SESSION_PERSONALIZATION_ENABLED` (default OFF). Flipping it ON in prod is a HUMAN decision and is a NO-OP until `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is also ON (which populates `apollo_learner_state` and is itself gated on a human calibration review). Before any prod flag flip, an operator should confirm the `event=personalized_selection` log appears with `fallback_fired=false` / `n_weak_entities>0` on a session with seeded learner state (i.e. the wedge actually engages rather than silently degrading).
