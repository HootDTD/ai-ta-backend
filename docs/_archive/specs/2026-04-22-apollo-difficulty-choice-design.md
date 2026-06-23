# Apollo Difficulty Choice & Problem Switching — Design Spec

**Date:** 2026-04-22
**Branch:** ApolloV2
**Status:** Draft awaiting user approval
**Parent spec:** [2026-04-21-apollo-teaching-rigor-design.md](2026-04-21-apollo-teaching-rigor-design.md), lines 184-186

## 1. Problem Framing

Phase 2 of the teaching-rigor spec shipped the XP + level machinery and
per-attempt difficulty persistence. The mechanics work — `handle_done`
already reads `attempt.difficulty` and awards XP correctly. The gap is
that **the student never actually chooses**:

- `apollo/hoot_bridge/session_init.py` hardcodes
  `_DEFAULT_FIRST_DIFFICULTY = "intro"`. Every session starts at intro.
- The `POST /apollo/sessions/from_hoot` request body has no `difficulty`
  field.
- There is no "next problem" endpoint. Today's loop is teach →
  `/done` → `/retry` (same problem) or `/end`. A new problem requires a
  new Hoot handoff.
- The student UI has no difficulty picker.

This spec closes that gap and extends the student's freedom further:
**the student should never be locked into a problem.** They can restart
their teaching on the current problem, or switch to a new problem (at any
difficulty) at any point, not just after grading.

The canonical difficulty vocabulary is the three tiers already shipped in
code: `intro` (×1.0), `standard` (×1.5), `hard` (×2.0). The four-tier
"Beginner / Easy / Intermediate / Challenging" naming in the parent spec
never landed and is not reintroduced.

**What we're building:**

1. Accept `difficulty` on the Hoot handoff and honor it for the first
   problem.
2. New `POST /apollo/sessions/{id}/next` endpoint that advances to a
   fresh problem at a student-chosen difficulty. Unified: works after
   `/done` (normal advance) and mid-problem (abandon + switch).
3. New `POST /apollo/sessions/{id}/restart_problem` endpoint that wipes
   the current attempt's knowledge-graph entries and chat messages,
   keeping the same problem and difficulty — a clean slate for a retried
   teaching approach.
4. Migrate `apollo_kg_entries` and `apollo_messages` to be
   attempt-scoped, not session-scoped, so multiple attempts can coexist
   cleanly within one session.
5. Frontend picker surfaces for all four entry points: pre-handoff,
   post-Done, mid-problem switch, and restart.

**What we're NOT changing:**

- XP formula, level tiers, or multiplier table.
- Rubric, solver, parser, coverage, diagnostic, Apollo persona.
- Problem JSON schema or the `intro | standard | hard` enum.
- The Hoot → Apollo handoff contract beyond the new `difficulty` field.
- The `/retry` endpoint (different operation — preserves KG after
  grading).

## 2. Architecture & Components

### Backend: endpoints

- `POST /apollo/sessions/from_hoot` — **modified**. Request body gains
  `difficulty: "intro" | "standard" | "hard"`. `init_session_from_hoot`
  takes `difficulty` as a parameter and passes it to `select_problem`
  and the new `ProblemAttempt`. The `_DEFAULT_FIRST_DIFFICULTY` constant
  is deleted.
- `POST /apollo/sessions/{id}/next` — **new**. Request body
  `{"difficulty": "..."}`. Unified "give me a new problem" endpoint.
  Server forks on `session.phase`:
  - `REPORT` → normal advance; prior attempt already graded.
  - `TEACHING` / `PROBLEM_REVEAL` → abandon path; mark prior attempt
    `result='abandoned'`.
  - `SOLVING` → 409 `session_frozen` (grading in flight; client retries).
  - `INIT` / `BETWEEN` → 409 `invalid_phase` (guard; unreachable in
    normal flow).
- `POST /apollo/sessions/{id}/restart_problem` — **new**. No body. Wipes
  the current attempt's KG entries and messages. Same problem, same
  difficulty, same `ProblemAttempt` row. Blocked with 409
  `session_frozen` during `SOLVING`.

### Backend: data model

- `apollo/persistence/models.py` — add nullable `attempt_id` column
  (`BigInteger`, FK to `apollo_problem_attempts.id` with
  `ON DELETE CASCADE`, indexed) to both `KGEntry` and `Message`.
- `apollo/knowledge_graph/store.py` — KGStore becomes attempt-scoped.
  `write_entries`, `read_kg`, and `summarize_for_apollo` take
  `attempt_id` instead of `session_id`. `_ensure_unfrozen` / `freeze` /
  `unfreeze` remain `session_id`-scoped because phase lives on the
  session.
- Migration: nullable columns, backfilled by joining each legacy row to
  the single existing attempt per session. Rollback = drop the columns.

### Backend: handlers touched

- `apollo/handlers/chat.py` — resolve current attempt, write messages
  with `attempt_id`.
- `apollo/handlers/done.py` — resolve current attempt at the top of the
  handler, thread `attempt_id` through KGStore calls.
- `apollo/handlers/lifecycle.py` — `handle_get_session` scopes KG and
  messages to the current attempt.
- `apollo/persistence/attempt_history.py` — **bug fix**
  `has_prior_graded_attempt` changes its filter from
  `result IS NOT NULL` to `result IN ('solved', 'stuck', 'skipped',
  'returned_to_hoot')` so that `abandoned` attempts don't get counted
  as prior grades.

### Backend: errors

- Reuse existing `PoolExhaustedError` (already 409 with
  `concept_cluster_id` + `difficulty` in payload) for exhausted pools.
- Reuse existing `SessionFrozenError` for `/next` and
  `/restart_problem` during `SOLVING`.
- A new `InvalidPhaseError` (409) covers `/next` called from `INIT`
  or `BETWEEN`. Small surface; a guard, not a user-facing flow.

### Frontend (`ai-ta-student-ui`)

- **Shared picker primitive:** three tiers (`intro`, `standard`, `hard`)
  with XP-multiplier badges. Every tier always selectable. No gating,
  no recommendation.
- **Surface 1 — Pre-handoff picker:** renders when Apollo mounts after
  a Hoot handoff, *before* calling `/from_hoot`. On selection, call
  `/from_hoot` with `hoot_transcript` + `difficulty`.
- **Surface 2 — Post-Done picker:** on the report screen, below the
  rubric card. Defaults to the just-graded difficulty. One click → `/next`.
- **Surface 3 — "Switch problem" button:** persistent during TEACHING
  / REPORT. Opens a modal with the picker + confirmation copy
  (*"This problem won't be graded…"*). Confirm → `/next`.
- **Surface 4 — "Restart this problem" button:** persistent during
  TEACHING / REPORT. Opens a smaller confirm modal (no picker; same
  difficulty). Confirm → `/restart_problem`.

### Not touched

XP module, rubric, solver, parser, coverage, diagnostic, Apollo persona,
problem JSON schema, teacher UI, `/retry` endpoint.

## 3. Data Flow

### `POST /apollo/sessions/from_hoot` (modified)

**Request:** `{student_id, hoot_transcript, difficulty}`

1. Validate `difficulty ∈ {intro, standard, hard}` (Pydantic `Literal`).
2. Run concept inference on the transcript.
3. `select_problem(cluster_id, difficulty, attempted_ids=[])`.
4. End any prior active session for this student.
5. Create `ApolloSession` (phase=TEACHING), create `ProblemAttempt` with
   the chosen difficulty, commit.
6. Return `{session_id, problem, attempt_id}`.

### `POST /apollo/sessions/{id}/next` (new)

**Request:** `{difficulty}`

1. Load session. Require `status='active'` else 409.
2. Resolve current attempt (latest for `session_id` where
   `problem_id == session.current_problem_id`).
3. Fork on `session.phase`:
   - `REPORT` → no mutation to prior attempt. (Step 6 sets phase →
     TEACHING, which is what unfreezes KGStore for the new attempt.)
   - `TEACHING` / `PROBLEM_REVEAL` → set
     `current_attempt.result = 'abandoned'`. No grading, no XP.
   - `SOLVING` → 409 `session_frozen`.
   - `INIT` / `BETWEEN` → 409 `invalid_phase`.
4. Build `attempted_ids = [p.problem_id for p in session.problem_attempts]`.
5. `select_problem(cluster_id, difficulty, attempted_ids)`. If
   `PoolExhaustedError` → existing 409 handler.
6. Create new `ProblemAttempt` (new problem, new difficulty, `result=NULL`).
   Set `session.current_problem_id = new.problem_id`,
   `session.phase = TEACHING`. Commit.
7. Return `{session_id, problem, attempt_id}`.

**Key invariant:** the abandoned attempt's KG and messages persist
unchanged because they're keyed on the old `attempt_id`. The new attempt
starts with zero KG / messages. This is the invariant the schema
migration buys.

### `POST /apollo/sessions/{id}/restart_problem` (new)

**Request:** no body.

1. Load session. Require `status='active'` else 409.
2. Require `phase != SOLVING` else 409 `session_frozen`. Allowed in
   TEACHING, PROBLEM_REVEAL, REPORT.
3. Resolve current attempt.
4. `DELETE FROM apollo_kg_entries WHERE attempt_id = :current_attempt_id`.
5. `DELETE FROM apollo_messages WHERE attempt_id = :current_attempt_id`.
6. Set `session.phase = TEACHING`. Commit.
7. Return `{ok: true}`.

**Key invariant:** same `ProblemAttempt` row, `result=NULL` preserved,
same `problem_id`, same `difficulty`. XP re-attempt detection is
unaffected — the attempt's grading identity hasn't changed.

### Response shapes

| Endpoint | 200 body |
|---|---|
| `/from_hoot` | `{session_id, problem, attempt_id}` |
| `/next` | `{session_id, problem, attempt_id}` |
| `/restart_problem` | `{ok: true}` |
| `/retry` | `{ok: true}` *(unchanged)* |
| `/done` | existing shape *(unchanged)* |

## 4. Schema Migration

### Columns added

- `apollo_kg_entries.attempt_id` — `BigInteger`, nullable, FK to
  `apollo_problem_attempts.id` with `ON DELETE CASCADE`, indexed.
- `apollo_messages.attempt_id` — same shape.

### Backfill

```sql
UPDATE apollo_kg_entries
SET attempt_id = (
  SELECT id FROM apollo_problem_attempts
  WHERE session_id = apollo_kg_entries.session_id
  ORDER BY id DESC LIMIT 1
)
WHERE attempt_id IS NULL;

UPDATE apollo_messages
SET attempt_id = (
  SELECT id FROM apollo_problem_attempts
  WHERE session_id = apollo_messages.session_id
  ORDER BY id DESC LIMIT 1
)
WHERE attempt_id IS NULL;
```

Today every session has exactly one attempt, so the join is
unambiguous.

### Why nullable

Backfill is best-effort. Leaving the column nullable means a stray
legacy row with no matching attempt becomes a null — invisible to
KGStore reads (`WHERE attempt_id = :id`) — rather than aborting the
migration. A NOT NULL tightening can land as a follow-up slice once the
column is validated in production.

### Rollback

Drop the two columns. KGStore code reverts to session-scoped
signatures. No data loss.

## 5. Error Handling & Edge Cases

### New and reused error surfaces

- **Unknown difficulty** (`/from_hoot`, `/next`): 422 via Pydantic
  `Literal["intro", "standard", "hard"]`. No free-form strings reach
  `select_problem` or `compute_xp_earned`.
- **Pool exhausted at requested difficulty**: existing
  `PoolExhaustedError` → 409 with `concept_cluster_id` and `difficulty`.
  No auto-ramp, no silent fallback. UI keeps the picker open.
- **`/next` or `/restart_problem` during `SOLVING`**: 409
  `session_frozen` (reuses existing error).
- **`/next` during `INIT` / `BETWEEN`**: 409 `invalid_phase` (new
  code; edge guard).
- **Session ended**: 409 via existing mechanisms.

### Bug surfaced in existing code (must fix this slice)

`apollo/persistence/attempt_history.py::has_prior_graded_attempt`
filters `result IS NOT NULL`. With `result='abandoned'` introduced,
abandoned attempts would incorrectly count as prior grades and trigger
the 25% re-attempt multiplier on the next real grade. **Fix:** change
the filter to an allowlist —
`result IN ('solved', 'stuck', 'skipped', 'returned_to_hoot')`. Future
non-grading `result` values are then safe by default.

### Race conditions

- **Double-clicked `/next`**: could race into two `ProblemAttempt`
  rows. Mitigation: do phase check, abandon-marking, and new-attempt
  creation in a single transaction. For Postgres, `SELECT ... FOR
  UPDATE` on the session row. SQLite tests get transactional isolation
  for free.
- **Concurrent `/chat` and `/restart_problem`**: a message in flight
  during a restart could land as the first message of the fresh
  conversation. Acceptable; the chat is already keyed on `attempt_id`
  and restart doesn't touch the attempt row. Not worth locking for.

### Abandon-path invariants

- An abandoned `ProblemAttempt` is read-only from that point. No code
  path mutates `result` once set.
- `handle_done` never operates on abandoned attempts — it resolves the
  current attempt via `session.current_problem_id`, which always points
  to the live attempt after `/next`.
- Abandoned attempts' `problem_id`s are included in `attempted_ids`
  passed to `select_problem`, so switching difficulty does not re-serve
  the same problem.

### Boundary cases

- **Empty KG on `/done` after abandon**: irrelevant; abandoned attempts
  don't flow to `/done`.
- **`/restart_problem` when KG already empty**: DELETEs match 0 rows;
  200 OK.
- **`/next` immediately after `/from_hoot`**: legal. Abandons an empty
  attempt and creates a new one. Student changed their mind.
- **`/restart_problem` then `/next`**: restart empties the current
  attempt; `/next` then abandons it and creates a new one. Consistent.

## 6. Testing

All tests use in-memory SQLite via the existing `conftest.py` fixtures.
No live LLM calls.

### Unit tests

- `apollo/knowledge_graph/tests/test_store.py` *(extended)* — KGStore
  reads/writes are attempt-scoped. Entries written under attempt A are
  invisible when reading under attempt B in the same session.
- `apollo/persistence/tests/test_attempt_history.py` *(extended)* —
  `has_prior_graded_attempt` excludes `result='abandoned'` attempts.
- `apollo/hoot_bridge/tests/test_session_init.py` *(extended)* —
  `init_session_from_hoot` requires `difficulty`; rejects unknown
  values; honors each of `intro`/`standard`/`hard`; created
  `ProblemAttempt.difficulty` matches the param.

### Handler tests

- `apollo/handlers/tests/test_next.py` *(new)*:
  - Post-REPORT advance: prior attempt stays graded; new attempt at
    requested difficulty; phase → TEACHING.
  - Mid-TEACHING abandon: prior attempt gains `result='abandoned'`;
    prior KG + messages remain tied to old `attempt_id`; new attempt
    starts empty.
  - `SOLVING` → 409 `session_frozen`.
  - `PoolExhaustedError` propagates as 409.
  - `attempted_ids` includes abandoned attempts' problem_ids.
  - Unknown difficulty → 422.
- `apollo/handlers/tests/test_restart_problem.py` *(new)*:
  - From TEACHING: KG + messages deleted for current attempt; attempt
    row unchanged; phase stays TEACHING.
  - From REPORT: same, plus phase unfreezes to TEACHING.
  - `SOLVING` → 409.
  - Other attempts' KG/messages in the same session are untouched.
- `apollo/handlers/tests/test_done.py` *(extended)* — an attempt with
  `result='abandoned'` elsewhere in the session is NOT counted toward
  `is_reattempt` for a fresh Done.

### Integration / e2e

- `apollo/tests/test_e2e_smoke.py` *(extended)* — multi-attempt flow:
  1. `/from_hoot` with `difficulty='intro'` → attempt A.
  2. Chat a couple of turns.
  3. `/next` with `difficulty='standard'` from TEACHING → attempt B;
     attempt A abandoned.
  4. Assert attempt A still has its KG/messages; attempt B is empty.
  5. Complete attempt B through `/done`; assert `xp_earned` uses
     `standard` multiplier and `is_reattempt=false` despite abandoned A.
  6. `/restart_problem` on attempt B after report → KG + messages
     wiped; attempt row persists.

### Migration test

- Test the backfill against a seeded legacy state: one session, one
  attempt, KG rows + messages with only `session_id`. After running the
  migration backfill, every row has `attempt_id` pointing to the
  existing attempt.

### Out of scope

- Frontend picker unit tests (lives in the student-UI repo; out of
  scope for this spec).
- `NOT NULL` tightening on `attempt_id` (deliberate follow-up slice).

## 7. Frontend Surfaces

All four surfaces live in `ai-ta-student-ui`. Teacher UI is untouched.

### Shared picker primitive

Three options: `intro`, `standard`, `hard`. Each rendered with its
display label, a small XP-multiplier badge (`×1.0`, `×1.5`, `×2.0`),
and a one-line description. No level-gating, no greyed-out tiers, no
recommendation — every tier is always selectable. "Motivation, not
coercion." One component, reused across all four surfaces.

### Surface 1 — Pre-handoff picker

Renders when Apollo mounts after a Hoot handoff, before calling
`/from_hoot`. Short framing ("ready to start teaching?") around the
picker; one CTA "Start teaching". On click, call `/from_hoot` with
`hoot_transcript` + `difficulty`.

### Surface 2 — Post-Done picker

Below the rubric card on the report screen, above a "Next problem"
button. Defaults to the difficulty of the just-graded attempt so the
common path — keep going at the same level — is one click. Selection
+ click → `/next`. The existing "Teach more and retry" button (calls
`/retry`) is unchanged.

### Surface 3 — "Switch problem" button

Persistent during TEACHING / REPORT, in the Apollo session header or
sidebar (same spot as "End session"). Opens a modal containing the
picker and the copy *"This problem won't be graded. Pick a new
difficulty to start over with a different problem."* Two actions:
"Switch problem" (primary, destructive styling) and "Cancel". Confirm
→ `/next`.

### Surface 4 — "Restart this problem" button

Persistent during TEACHING / REPORT, near Surface 3 but visually
distinct. No picker — restart keeps the same difficulty. Opens a
smaller confirm modal *"Wipe your teaching for this problem and start
over? Same problem, same difficulty."* Two actions: "Restart" (primary,
destructive styling) and "Cancel". Confirm → `/restart_problem`.

### Error surfacing

- 409 `pool_exhausted` on `/next` → inline message on the picker:
  *"No more unattempted problems at {difficulty}. Pick another level."*
  Keep picker open.
- 409 `session_frozen` on `/next` or `/restart_problem` → transient
  toast: *"Grading in progress — try again in a moment."*

## 8. Implementation Phasing

One slice, delivered in dependency order:

1. **Schema migration** — nullable `attempt_id` columns + backfill.
2. **KGStore + callers** — flip to attempt-scoped signatures.
3. **`has_prior_graded_attempt` bug fix** — allowlist filter.
4. **`/from_hoot` modification** — accept and honor `difficulty`.
5. **`/next` endpoint + handler** — unified advance/abandon.
6. **`/restart_problem` endpoint + handler** — KG + message wipe.
7. **Tests** — unit, handler, e2e, migration.
8. **Frontend** — picker primitive + four surfaces.

Backend can land and ship before the frontend (endpoints would just sit
unused). Frontend depends on all backend surfaces being in place.
