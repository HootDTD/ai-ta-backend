# Plan: WU-3D — §8A Course→Apollo runtime cutover (DB-resolved, course-scoped curriculum)

**Goal:** Cut the Apollo runtime over from three hard-coded curriculum maps + filesystem reads to a single course-scoped DB resolution: build the candidate-concept list from `apollo_concepts WHERE …search_space_id = :sid`, populate `apollo_sessions.concept_id`, stop writing `concept_cluster_id`, and resolve problems / concept machinery from the DB.
**Architecture:** Python 3.12 / FastAPI / SQLAlchemy async (asyncpg) / pytest + pytest-asyncio + Testcontainers (pgvector pg16). Apollo subsystem only (`apollo/**` + one migration in `database/migrations`).
**Branch (already checked out):** `feat/apollo-kg-wu3d-runtime-cutover` — do NOT create/switch branches, do NOT push, do NOT open PRs (GitHub MCP auth is down anyway; orchestrator pushes later).
**Coverage gate:** diff-cover vs `feat/apollo-kg-wu3c2-resolver`, patch coverage ≥ 95% (target 100%). DB real-infra gate runs green-not-skipped.

---
provides:
  - `apollo.subjects.curriculum_db.load_concept_definition(db, concept_id) -> ConceptDefinition` (async, DB-backed concept loader)
  - `apollo.subjects.curriculum_db.list_course_concepts(db, search_space_id) -> list[ConceptRow]` (course-scoped candidate concepts)
  - `apollo.overseer.problem_selector.list_problems_for_concept` / `select_problem` (concept_id + difficulty, async, DB-backed)
  - `apollo.overseer.concept_inference.infer_concept_id` (course concept rows -> concept_id)
  - `ApolloSession.concept_id` populated at session_init; `concept_cluster_id` no longer written
  - migration 027 dropping `apollo_sessions.concept_cluster_id` (once unused)
consumes:
  - `apollo_subjects.search_space_id` (migration 026 / WU-3A) — the §8A.3 structural link
  - `apollo_concepts` / `apollo_concept_problems` rows (seeded by the registry seeder + WU-3B)
  - `apollo_concept_problems.payload` carrying the full Problem JSON incl. `reference_solution` (criterion 7)
  - `ApolloSession.concept_id` FK (migration 018)
depends_on:
  - migration 026 (WU-3A) — applied to local Docker only by feller; structural link EXISTS
  - WU-3B seed — entities/concepts/problems present; payload carries reference_solution
  - `scripts/seed_apollo_concept_registry.py` — writes apollo_subjects/concepts/concept_problems rows the runtime reads
---

## Overview

§8A.1 names three hard-codings plus one missing structural link. The structural
link (`apollo_subjects.search_space_id`) already shipped in migration 026 (WU-3A),
so this unit is the §8A.4 *runtime cutover* only:

| § ref | Today (hard-coded) | After this unit |
|---|---|---|
| `session_init.py:23` | `_AVAILABLE_CLUSTERS = ["fluid_mechanics"]` | candidate concepts = `apollo_concepts` rows for `search_space_id` via `apollo_subjects` |
| `problem_selector.py:23` | `_CLUSTER_TO_CONCEPT` map + filesystem `load_concept`/`load_problem` glob | `apollo_concept_problems` rows by `concept_id` + `difficulty` (DB) |
| `subjects/__init__.py:108` | `load_concept(subject_id, concept_id)` reads disk JSON/MD | `load_concept_definition(db, concept_id)` builds `ConceptDefinition` from `apollo_concepts` JSONB/TEXT columns |
| `models.py:216` | `apollo_sessions.concept_cluster_id` (TEXT) written every session; `concept_id` FK NULL | `concept_id` populated; `concept_cluster_id` no longer written, then DROPPED in migration 027 |

The runtime read path becomes one scoped chain, every hop filtered by
`search_space_id`:

```
search_space_id
  └─► apollo_subjects   (WHERE search_space_id = :sid)
        └─► apollo_concepts          ← concept_inference candidate list (id + display_name)
              ├─► apollo_concept_problems   ← problem_selector by difficulty
              └─► apollo_misconceptions     ← already concept_id-scoped (not touched here)
```

Acceptance criteria mapping (§8A.6):

- #1 (`search_space_id` NOT NULL/backfilled, per-course uniqueness) — DONE in WU-3A; re-asserted by the isolation test, no new work.
- #2 (`_AVAILABLE_CLUSTERS`/`_CLUSTER_TO_CONCEPT`/`cluster_to_concept`/filesystem reads in selection path deleted; grep-clean) — Task 6.
- #3 (two courses, same physics, student sees only own course) — Task 8 isolation test.
- #4 (`session_init` populates `concept_id`, no `concept_cluster_id` write) — Task 4 + Task 8b.
- #5 (new course wired with zero code changes) — structural outcome of Tasks 1–6; asserted by Task 8 using TWO seeded courses with no per-course code.
- #6 (every curriculum read filters by `search_space_id`) — enforced by Task 8; concept resolution is the only entry point and it scopes; concept→problem→misconception inherit ownership through FKs.
- #7 (reference solutions live in `apollo_concept_problems.payload`) — already true (the registry seeder writes the full Problem JSON, `reference_solution` included, into `payload`). Task 0 asserts it defensively; no seed change unless the assertion fails.

## Prior art (sibling modules)

- **Async DB resolution + course scoping:** `apollo/auth_deps.py` `require_course_member` + `apollo/handlers/*.py` all take a keyword-only `db: AsyncSession` and `select(...).where(...)`; `apollo/knowledge_graph/canon_projection.py` `load_entity_specs(db, ...)` is the closest prior art for "async DB read returns frozen specs the rest of the code consumes" (pure mapping seam + async loader split). Mirror that split here.
- **DB-backed concept rows already modeled:** `apollo/persistence/models.py` `Concept` (`canonical_symbols`, `normalization_map`, `parser_prompt_template`, `solver_hints`, `forbidden_named_laws`, `concept_dag` columns) + `ConceptProblem` (`problem_code`, `difficulty`, `payload`) + `Subject` (`search_space_id`). The new loader maps a `Concept` row → existing `ConceptDefinition` pydantic model verbatim.
- **Seeder = the file→DB converter:** `scripts/seed_apollo_concept_registry.py` `_upsert_concept` writes exactly the six payload columns the loader reads; `_upsert_problem` writes `payload` = full problem JSON. The runtime mirror of that read is what Task 1/2 build.
- **Pure-vs-async split + immutability:** `apollo/persistence/learner_model_seed.py` (frozen dataclasses, `annotate_reference_solution` returns a NEW dict) is the immutability template.
- **Real-PG migration/behavioral test harness:** `tests/database/test_apollo_learner_model_migration.py` + `tests/database/test_seed_apollo_learner_model.py` (content-scoped migration chain → fresh DB on the session pgvector container, `auth.users`/`aita_search_spaces` stubs, per-test fresh DB). Tasks 7 and 8 mirror this harness exactly.
- **Function-scoped real-PG session fixture:** `tests/conftest.py` `db_session` (savepoint rollback per test, `Base.metadata.create_all`). Tasks 1–5 behavioral tests use this fixture (real PG, NOT SQLite — `_JSONType`/JSONB scoping queries must run on real PG, and the legacy SQLite handler tests are all module-skipped and reference the deleted `KGEntry` model — they are NOT a usable harness).
- **LLM mocking:** `apollo/overseer/tests/test_concept_inference.py` patches `apollo.overseer.concept_inference.OpenAI` with a `MagicMock` returning a canned `choices[0].message.content` JSON string. Reuse verbatim.

## Neighborhood scan (structural debt)

Files in the change path and their import counts (CBO proxy, threshold 8) /
exported-callable counts (WMC proxy, threshold 20):

- `apollo/hoot_bridge/session_init.py` — 4 imports, 1 public fn. Clean.
- `apollo/overseer/problem_selector.py` — 4 imports, 3 fns. Clean.
- `apollo/overseer/concept_inference.py` — 5 imports, 1 fn. Clean.
- `apollo/subjects/__init__.py` — 4 imports, ~6 fns. Clean (kept as the authoring/file format module; nothing removed there).
- `apollo/handlers/chat.py` — ~11 imports (> 8). This is a pre-existing hub and is NOT a WU-3D-introduced regression. We REMOVE two imports (`cluster_to_concept`, `list_problems_for_cluster`) and the `load_concept` filesystem call, NET-REDUCING coupling. No extraction undertaken — out of scope and the file is < 800 lines.
- `apollo/handlers/done.py` — ~13 imports (> 8). Same: we remove `list_problems_for_cluster`, net-reduce. No extraction.
- `apollo/handlers/next.py` / `lifecycle.py` — < 8 imports. Clean.

No circular imports introduced: the new `apollo/subjects/curriculum_db.py` imports
only `apollo.persistence.models` + `apollo.subjects` (`ConceptDefinition`) + SQLAlchemy;
`problem_selector` imports it; handlers import `problem_selector` + `curriculum_db`.
`apollo.subjects.__init__` does NOT import `curriculum_db` (one-way edge), so no cycle.

**Conclusion: neighborhood is clean. No structural-prep step.** The two handler
hubs already exceed the import threshold but this unit reduces their coupling;
adding an extraction step would exceed the 30% structural-prep budget and is
explicitly deferred.

## Sync→async decision (the §8A.4 wrinkle)

`load_concept`, `select_problem`, and `infer_concept_cluster`'s candidate load are
sync filesystem reads today, called from async handlers that already hold an
`AsyncSession`. The spec demands ONE shape applied uniformly.

**DECISION: async loaders that take `db: AsyncSession`.** The curriculum readers
become `async def …(db, …)`. Rationale:

1. Every call site already lives inside an `async def` handler holding `db`
   (`session_init`, `handle_next`, `handle_chat`, `handle_done`, `handle_get_session`).
2. The alternative ("handler pre-fetches rows, passes `ConceptDefinition`/problem
   rows in") still requires an async fetch in the handler AND threads two extra
   params through every helper (`_find_problem`, `_maybe_intent_confirmation`),
   widening signatures more than the async-loader shape.
3. The resolver/seed/canon prior art (`canon_projection.load_entity_specs(db, …)`,
   the seeder's `_upsert_*(session, …)`) all already use the "async fn takes db"
   shape — consistency.

Concrete signature impact (each call site passes its existing `db`):

- `session_init`: builds candidates via `list_course_concepts(db, search_space_id)`, infers `concept_id`, calls `await select_problem(db, concept_id=…, difficulty=…, attempted_ids=[])`. Already async.
- `handle_next`: `await select_problem(db, concept_id=sess.concept_id, …)`. Already async.
- `handle_chat` / `handle_done`: `_find_problem` becomes `async def _find_problem(db, concept_id, problem_code)`; `load_concept(subject_id, concept_id)` becomes `await load_concept_definition(db, sess.concept_id)`. Both handlers already async.
- `handle_get_session`: problem lookup becomes `await list_problems_for_concept(db, sess.concept_id)`. Already async.

No new threadpool / `asyncio.to_thread` is introduced — these are ordinary
asyncpg reads on the session already in scope.

## Public signatures (new + changed, backward-compat notes)

### NEW — `apollo/subjects/curriculum_db.py`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ConceptRow:
    """A candidate concept for a course (id + display name). The minimal shape
    concept_inference needs to pick among a course's concepts."""
    concept_id: int
    slug: str
    display_name: str

class ConceptNotFoundError(LookupError):
    """Raised when a concept_id has no apollo_concepts row (e.g. deleted course)."""

async def list_course_concepts(
    db: AsyncSession, *, search_space_id: int
) -> list[ConceptRow]:
    """All concepts a course teaches, scoped via apollo_subjects.search_space_id.
    JOIN apollo_concepts c ON c.subject_id = s.id WHERE s.search_space_id = :sid,
    ORDER BY c.id (deterministic). Returns [] when the course has no curriculum."""

async def load_concept_definition(
    db: AsyncSession, *, concept_id: int
) -> ConceptDefinition:
    """Build a ConceptDefinition (apollo.subjects.ConceptDefinition) from the
    apollo_concepts row's JSONB/TEXT columns. Raises ConceptNotFoundError if the
    row is absent. `subject_id`/`concept_id` fields of ConceptDefinition are set
    to the row's slug values for display continuity; `problems_dir` is set to a
    sentinel non-existent Path (runtime never globs it — problems come from
    list_problems_for_concept). canonical_symbols/solver_hints/forbidden_named_laws
    are re-validated through the same pydantic models load_concept used."""
```

Backward-compat note: `ConceptDefinition` is reused UNCHANGED so `parse_utterance`,
`compute_coverage`, `compute_rubric`, etc. (all consume `concept.*`) keep working.
`problems_dir` stays on the model (other code may reference it) but points at a
sentinel path; the runtime no longer reads it.

### CHANGED — `apollo/overseer/problem_selector.py`

```python
async def list_problems_for_concept(
    db: AsyncSession, *, concept_id: int
) -> list[Problem]:
    """Load every apollo_concept_problems row for a concept, validate each row's
    `payload` through Problem.model_validate (pydantic), sorted by Problem.id
    (== payload['id'] == problem_code) for determinism. NO filesystem read."""

async def select_problem(
    db: AsyncSession, *, concept_id: int, difficulty: str, attempted_ids: Sequence[str]
) -> Problem:
    """Pick the first unattempted Problem at `difficulty` for `concept_id`.
    Raises PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=…)
    when none remain (the error field name is unchanged for API back-compat —
    see Error contract decisions)."""
```

DELETED from this module: `_CLUSTER_TO_CONCEPT`, `cluster_to_concept`,
`list_problems_for_cluster` (and the `from apollo.subjects import … load_concept`
+ `from apollo.schemas.problem import load_problem` filesystem imports).
`Problem` / `PoolExhaustedError` imports stay. `Problem.id` remains the stable
author id (`payload["id"]`), so `ProblemAttempt.problem_id` and
`sess.current_problem_id` semantics are byte-identical (still the author string,
not the BIGSERIAL row id).

### CHANGED — `apollo/overseer/concept_inference.py`

```python
def infer_concept_id(
    *, transcript: str, candidates: list[ConceptRow], model: str | None = None
) -> int:
    """Isolated GPT-4o call. The LLM is given the transcript + the course's
    candidate concepts ({concept_id, display_name}); it returns exactly one
    concept_id (as int) from the provided set or null. Raises
    NoMatchingConceptError on null / unknown / invalid JSON. Stays a pure LLM
    call — only the candidate list changed from a constant to a scoped query
    result threaded in by the handler."""
```

Implementation detail: the prompt sends `[{"concept_id": …, "display_name": …}]`
and asks for `{"concept_id": <one of the ids, or null>}`. The returned id is
validated against `{c.concept_id for c in candidates}`; anything else →
`NoMatchingConceptError`. The old `infer_concept_cluster(transcript, available_clusters)`
is DELETED (no back-compat shim — no external caller; `session_init` is the only
caller and it is cut over in the same unit).

### CHANGED — `apollo/hoot_bridge/session_init.py`

`init_session_from_hoot` public signature is UNCHANGED
(`*, db, user_id, search_space_id, hoot_transcript, difficulty`). Internals:

```python
candidates = await list_course_concepts(db, search_space_id=search_space_id)
concept_id = infer_concept_id(transcript=hoot_transcript, candidates=candidates)
problem = await select_problem(
    db, concept_id=concept_id, difficulty=difficulty, attempted_ids=[],
)
...
session = ApolloSession(
    user_id=user_id,
    search_space_id=search_space_id,
    concept_id=concept_id,          # ← the 018 FK, now populated
    status=SessionStatus.active.value,
    phase=SessionPhase.TEACHING.value,
    current_problem_id=problem.id,
)
# concept_cluster_id is NOT set (defaults NULL; dropped in 027)
```

`_AVAILABLE_CLUSTERS` DELETED. `_ALLOWED_DIFFICULTIES` validation kept.
If `candidates == []` → `infer_concept_id` raises `NoMatchingConceptError`
(a course with no curriculum can't host a session — the existing 409 contract).

### CHANGED — consumer handlers

- `next.py:66`: `await select_problem(db, concept_id=sess.concept_id, difficulty=…, attempted_ids=…)`.
- `chat.py`: `_find_problem(db, sess.concept_id, sess.current_problem_id)` (async); `concept = await load_concept_definition(db, concept_id=sess.concept_id)` replaces `cluster_to_concept` + `load_concept`. The `from apollo.subjects import load_concept` + `from apollo.overseer.problem_selector import cluster_to_concept, list_problems_for_cluster` imports are removed.
- `done.py`: `_find_problem(db, sess.concept_id, sess.current_problem_id)` (async); remove `list_problems_for_cluster` import.
- `lifecycle.py:79`: `for p in await list_problems_for_concept(db, concept_id=sess.concept_id):`. The `concept_cluster_id` field in `handle_get_session`'s returned dict is replaced with `concept_id` (FE consumer impact — see Downstream consumers).

## Layered TDD tasks (ORDER MATTERS)

Each task: write the REAL test(s) first (RED), then minimal implementation (GREEN),
then refactor. No skip/xfail/empty-assert. LLM mocked deterministically; no live API.
`black`/`isort`/`ruff` after each file. Run `pytest <path> -v` after each task; run
the DB-marked subset (`pytest -m integration tests/database apollo -v`) for the
real-PG tasks.

### 0. Pre-flight: confirm criterion-7 payload + coverage baseline

- [ ] No code yet. Verify the registry seeder already writes `reference_solution` into `apollo_concept_problems.payload` (it does: `_upsert_problem(payload=prob["payload"])` where `prob["payload"]` is the full problem JSON). This is asserted defensively by a test in Task 2 (`test_db_problem_payload_carries_reference_solution`), not a code change.
- [ ] Confirm `git merge-base HEAD feat/apollo-kg-wu3c2-resolver` resolves (the diff-cover compare branch exists locally). If not, FLAG to orchestrator — do not invent a base.
- Verify: `git rev-parse feat/apollo-kg-wu3c2-resolver` succeeds.

### 1. DB-backed curriculum loader — `apollo/subjects/curriculum_db.py`

- [ ] **Test first:** `apollo/subjects/tests/test_curriculum_db.py` (real-PG `db_session` fixture). Tests T1.1–T1.6 (see per-test list).
- [ ] Implement `ConceptRow` (frozen dataclass), `ConceptNotFoundError`, `list_course_concepts`, `load_concept_definition`. Reuse `apollo.subjects.ConceptDefinition` + its `CanonicalSymbols`/`SolverHints`/`ForbiddenNamedLaws` pydantic validators on the JSONB columns. Immutable: build a NEW `ConceptDefinition`; never mutate the ORM row.
- File stays < 150 lines.
- Verify: `pytest apollo/subjects/tests/test_curriculum_db.py -v` green-not-skipped (Docker up).

### 2. problem_selector cutover — `apollo/overseer/problem_selector.py`

- [ ] **Test first:** rewrite `apollo/overseer/tests/test_problem_selector.py` to the new async DB API. Tests T2.1–T2.5.
- [ ] Implement async `list_problems_for_concept(db, *, concept_id)` (query `ConceptProblem` rows, `Problem.model_validate(row.payload)`, sort by `Problem.id`) and async `select_problem(db, *, concept_id, difficulty, attempted_ids)`. DELETE `_CLUSTER_TO_CONCEPT`, `cluster_to_concept`, `list_problems_for_cluster`, and the filesystem imports. Update the module docstring (no longer reads filesystem).
- Verify: `pytest apollo/overseer/tests/test_problem_selector.py -v`.

### 3. concept_inference candidate-list cutover — `apollo/overseer/concept_inference.py`

- [ ] **Test first:** rewrite `apollo/overseer/tests/test_concept_inference.py` to `infer_concept_id` with `ConceptRow` candidates. Tests T3.1–T3.5.
- [ ] Implement `infer_concept_id(*, transcript, candidates, model=None)`; update `_SYSTEM_PROMPT` to talk about concept ids + display names; validate the returned id against the candidate set. DELETE `infer_concept_cluster`.
- Verify: `pytest apollo/overseer/tests/test_concept_inference.py -v`.

### 4. session_init cutover — `apollo/hoot_bridge/session_init.py`

- [ ] **Test first:** NEW `apollo/hoot_bridge/tests/test_session_init_db.py` (real-PG `db_session`; the legacy `test_session_init.py` is module-skipped and references the deleted `KGEntry` — leave it untouched). Tests T4.1–T4.6.
- [ ] Implement: replace `infer_concept_cluster`/`_AVAILABLE_CLUSTERS`/sync `select_problem` with `list_course_concepts` + `infer_concept_id` + `await select_problem(db, …)`; set `concept_id=concept_id` on the `ApolloSession`, DROP the `concept_cluster_id=cluster_id` kwarg. Keep difficulty validation + stale-session ending.
- Verify: `pytest apollo/hoot_bridge/tests/test_session_init_db.py -v`.

### 5. Consumer handler cutover — `chat.py` / `done.py` / `next.py` / `lifecycle.py`

- [ ] **Test first:** NEW real-PG handler tests covering the cutover only (NOT a full grading re-test — those legacy tests are skipped). Tests T5.1–T5.6. Mock OpenAI/Neo4j as the existing handler-test pattern shows; for `chat`/`done` mock the heavy collaborators (`parse_utterance`, `compute_coverage`, `draft_reply`, `KGStore`, `Neo4jClient`) so the test isolates the CONCEPT-RESOLUTION path (the part this unit changes).
- [ ] Implement the four handler edits from "Public signatures → consumer handlers". `_find_problem` in chat.py and done.py becomes `async def _find_problem(db, concept_id, problem_code)` and matches on `Problem.id == problem_code`. `handle_get_session` returns `concept_id` instead of `concept_cluster_id`.
- Verify: `pytest apollo/handlers/tests/test_next_db.py apollo/handlers/tests/test_chat_concept_db.py apollo/handlers/tests/test_done_concept_db.py apollo/handlers/tests/test_lifecycle_db.py -v`.

### 6. Delete hard-codings + grep-clean assertion (criterion #2)

- [ ] **Test:** NEW `apollo/tests/test_no_hardcoded_curriculum.py` — a source-grep guard (Tests T6.1–T6.3). It reads the actual source files and asserts the deleted symbols / filesystem-read patterns are ABSENT from the selection path.
- [ ] By this task all deletions from Tasks 2–5 are done; this task is the executable proof. If the grep test fails, a deletion was missed — fix the source, not the test.
- Verify: `pytest apollo/tests/test_no_hardcoded_curriculum.py -v`.

### 7. Migration 027 (drop concept_cluster_id) + real-PG migration test

- [ ] **Test first:** NEW `tests/database/test_apollo_concept_cluster_drop_migration.py` mirroring `test_apollo_learner_model_migration.py`'s harness (content-scoped chain that touches `apollo_sessions`, + stubs, fresh DB on session pgvector container). Tests T7.1–T7.4.
- [ ] Write `database/migrations/027_apollo_drop_concept_cluster_id.sql`: `ALTER TABLE apollo_sessions DROP COLUMN IF EXISTS concept_cluster_id;` wrapped `BEGIN;…COMMIT;` with a header documenting the numbering caution (026 is on-disk top; 027 is next free; 023 two-file collision noted; LOCAL-DOCKER-ONLY; safe to drop because Tasks 4/6 prove nothing reads/writes it). NEVER applied to remote.
- [ ] Remove the `concept_cluster_id` column from `apollo/persistence/models.py` `ApolloSession` (models.py:216) in this SAME task, AFTER the migration test is green — ORM and migration drop together so `Base.metadata.create_all`-based tests stay consistent with the migrated schema.
- Verify: `pytest tests/database/test_apollo_concept_cluster_drop_migration.py -v` green-not-skipped.

### 8. Two-scoped-courses isolation test (criteria #3/#4/#5/#6) — `tests/database/test_apollo_curriculum_isolation.py`

- [ ] **Test (this IS the deliverable for #3/#6):** real-PG, the §8A.6 isolation contract. Build TWO courses (two `aita_search_spaces` rows), each with its OWN `apollo_subjects` + `apollo_concepts` + `apollo_concept_problems` rows — even the SAME physics topic ("Bernoulli") in both — using only data inserts (NO per-course code). Tests T8.1–T8.5.
- [ ] No new implementation — this proves Tasks 1–6 deliver isolation. If it fails, a scoping query is missing a `search_space_id` filter.
- Verify: `pytest tests/database/test_apollo_curriculum_isolation.py -v` green-not-skipped.

### Final gate

- [ ] `pytest apollo tests/database -v` (full, Docker up) — no skips on the new DB tests.
- [ ] `pytest --cov=apollo --cov=database --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3c2-resolver --fail-under=95` (target 100%).
- [ ] `black apollo database && isort apollo database && ruff check apollo database`.
- [ ] Owner-doc update committed in the same commit (drift contract).

## Per-test list (names + assertions + mocking)

**Shared test helper.** Add `_seed_course(session, *, slug, search_space_id,
concept_slug, problems)` to a small `apollo/subjects/tests/_curriculum_fixtures.py`
(or inline in each module — executor's choice). It does: insert an
`aita_search_spaces` row if needed → `Subject(slug, search_space_id)` →
`Concept(subject_id, slug, canonical_symbols/normalization_map/parser_prompt_template/
solver_hints/forbidden_named_laws/concept_dag from the bernoulli files OR minimal
valid dicts)` → one `ConceptProblem(concept_id, problem_code=payload['id'],
difficulty=payload['difficulty'], payload=payload)` per supplied problem. Returns
`(search_space_id, concept_id)`. Uses the real `db_session` fixture; no SQLite.
For canonical_symbols/solver_hints use minimal valid shapes (e.g.
`{"symbols": ["P","v"], "description": {}}`, `{}`) so the pydantic validators pass.
`aita_search_spaces` exists in `Base.metadata` (the `db_session` fixture does
`create_all`), so inserting rows is a plain ORM/`text()` insert.

### Task 1 — `apollo/subjects/tests/test_curriculum_db.py` (real-PG)

- **T1.1 `test_list_course_concepts_returns_only_that_courses_concepts`** — seed course A (sid=1) with concept `bernoulli` and course B (sid=2) with concept `bernoulli` (same slug, different course). Assert `list_course_concepts(db, search_space_id=1)` returns exactly course A's `concept_id` (one `ConceptRow`); the id is NOT course B's. Asserts the JOIN-on-`search_space_id` scoping. Mocking: none (pure DB).
- **T1.2 `test_list_course_concepts_empty_when_no_curriculum`** — sid with a search_space row but no subjects → `[]`.
- **T1.3 `test_list_course_concepts_ordered_by_id`** — seed two concepts in one course → returned `ConceptRow`s are ascending by `concept_id` (determinism).
- **T1.4 `test_load_concept_definition_builds_from_db_columns`** — seed a concept with known `canonical_symbols`/`normalization_map`/`parser_prompt_template`/`solver_hints`/`forbidden_named_laws`; assert the returned `ConceptDefinition` round-trips those (e.g. `cd.canonical_symbols.symbols == [...]`, `cd.parser_prompt_template == "..."`, `cd.forbidden_named_laws.all_terms()` non-empty when seeded).
- **T1.5 `test_load_concept_definition_raises_on_missing_concept`** — `load_concept_definition(db, concept_id=999999)` raises `ConceptNotFoundError`.
- **T1.6 `test_load_concept_definition_problems_dir_is_sentinel_not_globbed`** — assert `cd.problems_dir` does NOT exist on disk (`.exists() is False`) i.e. the runtime never reads files. Locks in criterion #2 (no filesystem read in the concept path).

### Task 2 — `apollo/overseer/tests/test_problem_selector.py` (rewrite, real-PG)

- **T2.1 `test_list_problems_for_concept_returns_db_problems`** — seed a concept + 3 problems (`payload` = real bernoulli problem JSONs). Assert `await list_problems_for_concept(db, concept_id=cid)` returns 3 `Problem`s with `.id == payload['id']`, sorted ascending.
- **T2.2 `test_list_problems_for_concept_scoped_to_concept`** — seed two concepts each with a problem; assert the call for concept A returns only A's problem (no cross-concept bleed).
- **T2.3 `test_select_problem_intro_excludes_attempted`** — seed ≥2 intro problems; `select_problem(..., attempted_ids=[first.id])` returns a different problem.
- **T2.4 `test_select_problem_raises_pool_exhausted`** — attempt all intro ids → `PoolExhaustedError` with `.difficulty == "intro"` and `.concept_cluster_id == str(cid)`.
- **T2.5 `test_db_problem_payload_carries_reference_solution`** (criterion #7) — seed a problem from a real bernoulli JSON; reload via `list_problems_for_concept`; assert `problem.reference_solution` is non-empty AND `problem.to_kg_graph(attempt_id=-1)` yields a non-empty node set (the §6 grading core reads this from the DB, not disk). Mocking: none.

### Task 3 — `apollo/overseer/tests/test_concept_inference.py` (rewrite)

Mocking: `@patch("apollo.overseer.concept_inference.OpenAI")` returning a `MagicMock`
whose `chat.completions.create` returns a canned `choices[0].message.content` JSON
(reuse the existing `_mock_reply` helper).

- **T3.1 `test_infer_returns_concept_id_from_candidates`** — candidates `[ConceptRow(11,"bernoulli","Bernoulli")]`, LLM returns `{"concept_id": 11}` → returns int `11`.
- **T3.2 `test_infer_raises_on_unknown_concept_id`** — LLM returns `{"concept_id": 999}` not in candidates → `NoMatchingConceptError`.
- **T3.3 `test_infer_raises_on_null`** — LLM returns `{"concept_id": null}` → `NoMatchingConceptError`.
- **T3.4 `test_infer_raises_on_invalid_json`** — LLM returns `"not json"` → `NoMatchingConceptError`.
- **T3.5 `test_infer_sends_candidate_display_names_to_llm`** — capture the `create(messages=…)` user content, assert each candidate's `display_name` + `concept_id` appears in the JSON sent (proves the course's concepts, not a constant, drive the prompt).

### Task 4 — `apollo/hoot_bridge/tests/test_session_init_db.py` (real-PG)

Mocking: `@patch("apollo.hoot_bridge.session_init.infer_concept_id")` (the LLM hop);
everything else hits real PG. Seed one course via `_seed_course`.

- **T4.1 `test_init_session_populates_concept_id`** (criterion #4) — `infer_concept_id` mocked to return the seeded `concept_id`; after `init_session_from_hoot`, the persisted `ApolloSession.concept_id == concept_id`.
- **T4.2 `test_init_session_does_not_write_concept_cluster_id`** (criterion #4) — assert the persisted session's `concept_cluster_id` is `None` (before Task 7 drops the column; after Task 7 the attribute no longer exists, so this test is written to assert via a raw `SELECT concept_id` and the absence of any cluster write — see note). NOTE: order Task 7 AFTER Task 4; this test asserts `concept_cluster_id IS NULL` while the column still exists, and is UPDATED in Task 7 to drop that assertion line when the column is dropped.
- **T4.3 `test_init_session_creates_first_attempt`** — `ProblemAttempt` row exists with `difficulty="intro"` and `problem_id == problem.id`.
- **T4.4 `test_init_session_ends_stale_active_session`** — two inits for same user → first session `ended`, second `active` (unchanged behavior, re-proved on DB curriculum).
- **T4.5 `test_init_session_candidates_passed_to_infer_are_course_scoped`** — spy on `infer_concept_id`; assert the `candidates` arg == `list_course_concepts(db, search_space_id)` (only this course's concepts).
- **T4.6 `test_init_session_rejects_unknown_difficulty`** — `difficulty="impossible"` → `ValueError` (validation preserved, before any LLM call).

### Task 5 — handler cutover tests (real-PG)

`apollo/handlers/tests/test_next_db.py`:
- **T5.1 `test_next_selects_problem_by_session_concept_id`** — seed course + session with `concept_id` set + a graded attempt in REPORT phase; `handle_next` advances and the new problem belongs to that concept. Mock nothing (DB-only path).
- **T5.2 `test_next_pool_exhausted_uses_concept_id`** — monkeypatch `select_problem` to raise `PoolExhaustedError` → propagates (unchanged 409 contract).

`apollo/handlers/tests/test_chat_concept_db.py`:
- **T5.3 `test_chat_loads_concept_definition_from_db`** — seed course + session (`concept_id` set); patch `parse_utterance`/`draft_reply`/`KGStore`/`build_graph_context` to no-op deterministic returns; assert `handle_chat` resolves the concept via `load_concept_definition` (e.g. `parse_utterance` is called with a `concept=` whose `parser_prompt_template` matches the seeded row). Mock OpenAI + Neo4j entirely.
- **T5.4 `test_chat_find_problem_matches_problem_code`** — the resolved `problem.problem_text` matches the seeded problem (proves `_find_problem` reads DB by concept_id + problem_code).

`apollo/handlers/tests/test_done_concept_db.py`:
- **T5.5 `test_done_resolves_problem_from_db_concept_id`** — seed course + session + attempt; patch `KGStore`/`compute_coverage`/`compute_rubric`/`generate_diagnostic`/`apply_xp`/`stamp_graded_at` deterministically; assert `handle_done` builds the reference graph from the DB problem (`problem.to_kg_graph` called; result `rubric` present). Mock OpenAI + Neo4j.

`apollo/handlers/tests/test_lifecycle_db.py`:
- **T5.6 `test_get_session_returns_concept_id_and_db_problem`** — seed course + session (`concept_id`); `handle_get_session` returns `concept_id` (not `concept_cluster_id`) and the problem dict from `list_problems_for_concept`. Mock Neo4j (`KGStore.read_graph`).

### Task 6 — `apollo/tests/test_no_hardcoded_curriculum.py` (source-grep guard)

Reads the actual source files via `Path(...).read_text()` and asserts patterns absent.

- **T6.1 `test_available_clusters_constant_deleted`** — `_AVAILABLE_CLUSTERS` not in `session_init.py` source.
- **T6.2 `test_cluster_to_concept_deleted`** — `_CLUSTER_TO_CONCEPT`, `def cluster_to_concept`, `list_problems_for_cluster` not in `problem_selector.py`; `cluster_to_concept`/`infer_concept_cluster` not imported in `chat.py`/`done.py`/`next.py`/`lifecycle.py`.
- **T6.3 `test_no_filesystem_concept_read_in_selection_path`** — `from apollo.subjects import` / `load_concept(` / `load_problem(` / `.glob(` absent from `problem_selector.py`, `session_init.py`, and the concept-resolution lines of the four handlers. (Scoped to the SELECTION path; `apollo/subjects/__init__.py` legitimately keeps its file readers as the authoring format module.)

### Task 7 — `tests/database/test_apollo_concept_cluster_drop_migration.py` (real-PG)

Harness mirrors `test_apollo_learner_model_migration.py`: content-scoped chain
(every migration touching `apollo_sessions`) applied to a fresh DB on the session
pgvector container, with `auth.users`/`aita_search_spaces` stubs, then 027.

- **T7.1 `test_027_drops_concept_cluster_id`** — after applying the chain + 027, `information_schema.columns` has NO `concept_cluster_id` on `apollo_sessions` and STILL has `concept_id`.
- **T7.2 `test_027_idempotent`** — applying 027 twice succeeds (`DROP COLUMN IF EXISTS`).
- **T7.3 `test_027_preserves_existing_sessions`** — insert a session row (with `concept_id`) before 027, apply 027, row survives with `concept_id` intact.
- **T7.4 `test_models_apollo_session_has_no_concept_cluster_id`** — `"concept_cluster_id" not in ApolloSession.__table__.columns` (ORM and migration dropped together).

### Task 8 — `tests/database/test_apollo_curriculum_isolation.py` (real-PG, the §8A.6 contract)

Build TWO courses with the SAME physics, data-only (no per-course code):
course X (sid=X) → subject `physics_x` → concept `bernoulli` → 2 problems;
course Y (sid=Y) → subject `physics_y` → concept `bernoulli` → 2 DIFFERENT problems.

- **T8.1 `test_student_in_course_x_offered_only_x_concepts`** (criterion #3) — `list_course_concepts(db, search_space_id=X)` returns only X's concept_id; Y's concept_id absent. Symmetric check for Y.
- **T8.2 `test_problems_scoped_to_course_concept`** (criterion #3/#6) — `list_problems_for_concept(db, concept_id=X_cid)` returns only X's problem_codes; none of Y's.
- **T8.3 `test_session_init_for_course_x_picks_x_problem`** (criterion #3/#4) — patch `infer_concept_id` to return X's concept_id given X's candidates; run `init_session_from_hoot(search_space_id=X)`; the created session's `concept_id == X_cid` and `current_problem_id` ∈ X's problem_codes. Mock the LLM hop only.
- **T8.4 `test_new_course_wired_with_zero_code`** (criterion #5) — the entire test adds course Y via INSERTs only and a full `init_session_from_hoot(search_space_id=Y)` succeeds end-to-end (LLM hop mocked) — no code path references Y by name. The assertion is structural: the same code that serves X serves Y.
- **T8.5 `test_every_curriculum_read_filters_by_search_space`** (criterion #6) — cross-call: `list_course_concepts(X)` ∩ `list_course_concepts(Y)` concept_ids == ∅; and a concept_id from course Y is never returned for a course-X query. Documents that the ONLY curriculum entry point (concept resolution) is course-scoped, and problem/misconception reads inherit scope through `concept_id` FKs.

## DI / wiring discovery findings

- **`AsyncSession` injection:** every handler + `init_session_from_hoot` receives `db` keyword-only from FastAPI `Depends(get_db_session)` (`database.session.get_db_session`). No provider registration changes — the new curriculum loaders are plain async functions taking the already-injected `db`. No new FastAPI dependency, no `api.py` change.
- **No new providers / no DI container:** this is FastAPI; "wiring" = the `apollo/api.py` route → handler call chain. The route signatures and `Depends` are UNCHANGED (handler public signatures unchanged). `api.py` is touched ONLY if needed for an exception-handler change — and it is NOT (PoolExhaustedError/NoMatchingConceptError handlers stay; see Error contract). **api.py is out of scope and untouched.**
- **Sibling convention for "async DB read returns specs":** confirmed against `canon_projection.load_entity_specs(db, *, …)` and the seeder's `_upsert_*(session, …)` — all keyword-`db`-first, `select(...).where(...)`, return frozen/pydantic objects. New code matches.
- **`aita_search_spaces` in test metadata:** it IS in `Base.metadata` (the `db_session` fixture `create_all`s it), so the isolation/seed-helper inserts are plain ORM/`text()` inserts — no stub needed for the function-scoped `db_session` tests. The migration-027 test (Task 7) uses the asyncpg stub harness instead (mirrors WU-3A) where `aita_search_spaces` is stubbed.

## Transaction scope decisions

- **`init_session_from_hoot` (Task 4):** unchanged transaction shape — one `db.commit()` after end-stale-session UPDATE + `ApolloSession` insert + `ProblemAttempt` insert (all in the session already open). The curriculum reads (`list_course_concepts`, `select_problem`) are READS on the same session/transaction before the writes; no separate transaction. The LLM `infer_concept_id` call sits BETWEEN the reads and the writes (as today) — no DB lock is held across the LLM call because the writes happen after it. No multi-table write semantics change.
- **Handlers (Task 5):** all reads added are on the existing `db`/transaction; no new write spans. `handle_next` keeps its `with_for_update()` row lock; `select_problem` is now a read on the same locked transaction (fine).
- **Migration 027 (Task 7):** single `ALTER TABLE … DROP COLUMN` in one `BEGIN;…COMMIT;`. Atomic by definition.
- **No external-service + DB write straddle introduced.** The only external call (OpenAI in `infer_concept_id`) is a pure read with no DB side effect; if it fails the transaction simply never commits the session row. No compensation logic needed.

## Error contract decisions

The client-facing error shape is UNCHANGED — no new exception class, no new handler:

- **`NoMatchingConceptError`** (existing, `apollo/errors.py`) → 409 `no_matching_concept` via `no_matching_concept_handler` in `api.py`. Now raised by `infer_concept_id` (empty/unknown/null/invalid-JSON), including the new "course has no curriculum → empty candidates → null" path. Same message + `transcript_summary` field. No change to the handler.
- **`PoolExhaustedError`** (existing) → 409 `pool_exhausted`. Its `concept_cluster_id` constructor field is RETAINED (now passed `str(concept_id)`) so the existing `pool_exhausted_handler` JSON shape (`concept_cluster_id`, `difficulty`) is byte-identical — the frontend error handler does not break. (Renaming that field is explicitly OUT OF SCOPE; it would change the API response and ripple to the UIs.)
- **`ConceptNotFoundError`** (NEW, in `curriculum_db.py`) — an INTERNAL error, deliberately NOT registered as an HTTP handler (matches the WU-3C1/3C2 precedent of internal named errors that surface as raw 500 if they ever reach a request). It only fires when `sess.concept_id` points at a deleted concept (a should-never-happen given the `ON DELETE RESTRICT` FK), so it is a loud bug-surfacing error, not a user-facing contract. Documented in the owner doc's conventions section.
- **NO FALLBACK preserved:** no silent default-concept, no "pick the first course" guess. An unscoped/empty resolution raises.

## Owner-doc updates (drift contract)

Edit `docs/architecture/apollo.md` IN THE SAME COMMIT, set `last_verified: 2026-06-17`.
Concrete edits:

1. **Frontmatter:** `last_verified: 2026-06-16` → `2026-06-17`.
2. **Module map — `apollo/overseer/`:** `concept_inference = Hoot transcript → cluster_id` → `→ concept_id (from the course's apollo_concepts candidate rows)`; `problem_selector = authored problem bank loader` → `DB problem bank loader (apollo_concept_problems by concept_id + difficulty); no filesystem, no cluster map`.
3. **Module map — `apollo/hoot_bridge/`:** `transcript → concept inference → problem selection` → note it now resolves the candidate set from `apollo_concepts WHERE search_space_id` and populates `apollo_sessions.concept_id`.
4. **Module map — `apollo/subjects/`:** add `curriculum_db.py` (DB-backed `load_concept_definition`/`list_course_concepts`); change the trailing sentence "runtime still reads the filesystem registry via problem_selector.cluster_to_concept" → "runtime reads concepts/problems from the DB (`curriculum_db` + `problem_selector`); the filesystem layout is the AUTHORING source only, converted to rows by `scripts/seed_apollo_concept_registry.py`."
5. **Data flow (a) chat step 1:** `resolve concept via problem_selector.cluster_to_concept(sess.concept_cluster_id) → load_concept` → `load_concept_definition(db, sess.concept_id)`.
6. **Data flow (b) done step 3:** reference graph from `apollo_concept_problems.payload` (DB), not `problems/problem_*.json`.
7. **Data flow (c) hoot handoff:** rewrite the `_AVAILABLE_CLUSTERS`/`infer_concept_cluster` sentence to the DB-scoped candidate + `infer_concept_id` + `concept_id` population.
8. **Non-obvious conventions — "Hardcoded migration-window seams":** DELETE the `_AVAILABLE_CLUSTERS`/`_CLUSTER_TO_CONCEPT`/`concept_cluster_id` bullet (it is now FALSE); replace with a one-line note that the cutover landed in WU-3D (migration 027 dropped `concept_cluster_id`; curriculum is fully DB-resolved + course-scoped). Add `ConceptNotFoundError` to the NO-FALLBACK named-error list (internal, not HTTP-registered).
9. **HTTP surface / `from_hoot` row:** confirm the 409 list still references `NoMatchingConceptError`/`PoolExhaustedError` (unchanged — no edit needed beyond verifying).
10. **Persistence bullet:** note `concept_cluster_id` dropped (027); `apollo_sessions.concept_id` (018 FK) now populated at session creation.

Also update `database/migrations` references in `docs/architecture/domain-data.md`
ONLY if it enumerates migration numbers (verify: grep for "026"/"migration" — if a
migration table lists up to 026, append 027). If domain-data.md does not own the
migration list, no edit. (Verify before editing; do not touch unrelated docs.)

## Downstream consumers

- **`ai-ta-student-ui`** consumes `GET /apollo/sessions/{id}` and `POST /sessions/from_hoot`. Verified grep (2026-06-17):
  - `lib/apollo/api.ts:128` declares the SESSION type field `concept_cluster_id: string`. This is the genuine breaking change — `handle_get_session` now returns `concept_id: number` instead. This is a follow-up UI task (OUT OF SCOPE here; backend is the authority). FLAG to orchestrator: the student-UI session type needs `concept_cluster_id` → `concept_id` (and any consumer of it). Grep shows no behavioral use beyond the type + the error case below, so impact is contained.
  - `components/apollo/ApolloErrorSurface.tsx:43` reads `extra.concept_cluster_id` from the `pool_exhausted` ERROR payload (NOT the session object). That field is PRESERVED (passed `str(concept_id)`), so the error surface keeps working — it will display a numeric concept id instead of `"fluid_mechanics"`, a cosmetic-only change. Note this in the UI follow-up but it does not break.
  - `docs/architecture/components.md` references it descriptively — UI doc drift, fix in the UI follow-up.
  - The `problem` object shape (`id`, `concept_id`, `difficulty`, `problem_text`, `given_values`, `target_unknown`) is UNCHANGED, so teaching/grading FE paths are unaffected. NOTE: `problem.concept_id` (string author concept slug from the Problem schema) is DISTINCT from the session-level `concept_id` (int FK) — both keep their existing meaning; do not conflate them.
- **`from_hoot` 409 contracts** (`no_matching_concept`, `pool_exhausted`) unchanged in shape — student-UI error handling unaffected (the `pool_exhausted.concept_cluster_id` field is retained, see above).
- **§6 grading core / Layer-1 (future WU-4A)** consume `reference_solution` from `apollo_concept_problems.payload` — criterion #7, now load-bearing and tested (T2.5).
- The resolver (`apollo/resolution/`), `:Canon` projection, and `knowledge_graph` code from prior units are consumers of `concept_id`-scoped entities and are NOT modified here.

## Risks

- **[HIGH] `apollo_concept_problems` rows must exist for the seeded courses.** The runtime now hard-depends on the registry seeder (and/or WU-3B) having written rows. If a test DB has the migration but no curriculum rows, `list_course_concepts` returns `[]` → `NoMatchingConceptError`. Mitigation: every behavioral test seeds its own rows via `_seed_course`; production wiring is a data op (criterion #5). Confirm WU-3B/registry seed populates `payload` with `reference_solution` (T2.5 / Task 0).
- **[MEDIUM] `handle_get_session` response field rename** (`concept_cluster_id` → `concept_id`) is a contract change to the student UI. Mitigation: documented in Downstream consumers; grep the UI; the orchestrator decides whether a paired UI task is needed. Backend keeps the authoritative shape.
- **[MEDIUM] Legacy SQLite handler tests are module-skipped and reference the deleted `KGEntry` model.** Do NOT attempt to un-skip or reuse them — write NEW real-PG tests. Risk is wasted effort if the executor tries to revive `test_next.py`/`test_done.py`/`test_session_init.py`.
- **[MEDIUM] diff-cover base branch.** `feat/apollo-kg-wu3c2-resolver` must exist locally for the 95% gate. Task 0 verifies; if absent, FLAG rather than guess a base.
- **[LOW] `Problem.id` vs `problem_code` vs row id.** `Problem.id` == `payload['id']` == `ConceptProblem.problem_code` (the stable author string). `ProblemAttempt.problem_id` and `sess.current_problem_id` continue to store that string, NOT the BIGSERIAL row id — so re-attempt detection (`has_prior_graded_attempt`) and `_find_problem` matching stay correct. Tests T2.1/T5.4 lock this.
- **[LOW] `concept_inference` returning an int.** JSON round-trips ids as numbers; validate `isinstance(returned, int)` (and reject bool) before the membership check to avoid `True == 1` foot-guns.
- **[LOW] Migration numbering.** 027 is the next free on-disk number; the 023 two-file collision is historical and does not affect 027. Header documents LOCAL-DOCKER-ONLY.

## Out-of-scope boundaries

- NO changes to `apollo/api.py` (routes, exception handlers), `apollo/auth_deps.py`, or any auth path.
- NO changes to the resolver (`apollo/resolution/`), `:Canon` projection, `knowledge_graph/`, the §3 belief math, or the §6 grading core — consumed only.
- NO §8B materials→Apollo auto-provisioning (write side) — this unit is the READ side only.
- NO new exception class hierarchy beyond the single internal `ConceptNotFoundError`; NO renaming `PoolExhaustedError.concept_cluster_id` (API contract).
- NO deletion of `apollo/subjects/__init__.py` filesystem readers (`load_concept`, `load_problem`, `list_subjects`) — they remain the AUTHORING format used by the seeder. We only stop the SELECTION path from calling them. (If a grep shows `load_concept` has NO remaining caller after the cutover, leaving it is acceptable — deleting it is an optional cleanup the executor MAY do only if it stays test-green and within coverage.)
- NO `apollo_misconceptions` changes — already `concept_id`-scoped.
- NO remote DB / Supabase / Railway touches; migrations LOCAL-DOCKER-ONLY.
- NO new third-party packages.
- NO branch/PR/push operations.

## Deviations the executor may take

- **Test file placement:** the per-test grouping is a recommendation; the executor MAY consolidate the four Task-5 handler test files into fewer files if cohesion is better, as long as every named test exists and the real-PG marker is applied.
- **`_seed_course` helper location:** inline per-module vs a shared `_curriculum_fixtures.py` — executor's call; keep it < 60 lines and immutable (returns ids, no global state).
- **Minimal vs real bernoulli payloads in seeds:** prefer the REAL bernoulli problem JSONs for the problem-selector/grading tests (T2.5 needs a valid `reference_solution`); minimal valid dicts are fine for concept-only tests (T1.x).
- **`load_concept_definition` `problems_dir` sentinel:** any non-existent `Path` is acceptable (e.g. `Path("/__apollo_db_concept_no_fs__")`); the only contract is `.exists() is False`.
- **Prompt wording in `infer_concept_id`:** the executor MAY refine `_SYSTEM_PROMPT` text; the contract is: input carries `{concept_id, display_name}` per candidate, output is `{"concept_id": <int|null>}`, hallucinated/unknown → `NoMatchingConceptError`.
- If `git merge-base` against `feat/apollo-kg-wu3c2-resolver` is unavailable, STOP and signal `NEEDS_CONTEXT` rather than coverage-gating against a guessed base.

