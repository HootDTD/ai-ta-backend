# Spec: Apollo questioning redesign — persisted tally, honored done, guard→belt

Date: 2026-07-16 · Author: styx conductor (claude) · Implementer: codex
Repo: `ai-ta-backend` · Base: `origin/staging` (includes PR #184).
NEW worktree, branch `feat/apollo-tally-questioning`.

This implements the "this week" slice of the joint recommendation from the
design debate at
`styx/plans/2026-07-16-apollo-question-quality-brainstorm.md`
(§ "Round 2 — claude (joint recommendation)"). Read that section first — it
is the design authority; this spec is the executable cut. Copy BOTH files
into the branch: this spec AND the full debate doc, as
`docs/_archive/specs/2026-07-16-apollo-tally-questioning-spec.md` and
`docs/_archive/design/2026-07-16-apollo-question-quality-brainstorm.md`.

## Design summary (one paragraph)

Keep the single unified LLM call and give it MORE: persist the per-node
tally it already emits and feed it back each turn as structured memory;
honor its `action=done`; add a hard question budget; delete the machinery
that overrides/censors it (done-override, echo guard, vocabulary allowlist,
broad-reask, reject→retry, canned fallback chain); reduce privacy
enforcement to a three-atom-class output belt with one bounded regenerate.
Objective reframed: maximize what the student reveals — open untouched
private-node territory with naive safe questions. Net-negative LOC expected.

## Changes — all in `ai-ta-backend`

### 1. Migration 047 + model: persisted tally

`database/migrations/047_apollo_question_tally.sql` + SQLAlchemy model
(`apollo/persistence/models.py`):

```sql
CREATE TABLE apollo_question_tally (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL REFERENCES problem_attempts(id) ON DELETE CASCADE,
    reference_node_id TEXT NOT NULL,
    status TEXT NOT NULL,                 -- understood|tentative|conflicting|missing
    evidence JSONB NOT NULL DEFAULT '[]', -- [{"turn_id": int, "quote": str}]
    student_declined BOOLEAN NOT NULL DEFAULT FALSE,
    times_asked INTEGER NOT NULL DEFAULT 0,
    last_asked_turn INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, reference_node_id)
);
CREATE INDEX ix_apollo_question_tally_attempt ON apollo_question_tally (attempt_id);
```

Follow existing migration file conventions (look at 042/046). NEVER apply to
any remote DB — file + local testing only; deploying is a human step.
Budget spent is derived: `SUM(times_asked)` per attempt — no separate
counter. Optimistic versioning/idempotency is explicitly OUT of scope
(later hardening per the joint recommendation).

### 2. `unified.py` — the one call, new inputs/outputs

Payload: keep the per-attempt-stable fields FIRST and byte-stable
(public_problem, public_question_parts, private_reference_nodes,
private_reference_edges) — the prompt-cache key-order test must keep
passing. Append per-turn fields at the END: replace `question_history` with
`tally_state`, keep `transcript` last, add `budget`.

- `tally_state`: list of `{node_id, label, status, evidence, student_declined,
  times_asked, last_asked_turn}` for every reference node (missing rows
  default to status="missing", times_asked=0).
- `budget`: `{questions_asked, cap}`.

Schema (structured output) changes:
- Keep the existing per-node coverage output (this IS `tally_updates` —
  same taxonomy understood|tentative|conflicting|missing, same
  `_validated_evidence` verbatim-quote check at unified.py:363; keep quotes,
  no char offsets).
- Add optional per-node boolean `student_declined` — set true when the
  student explicitly disclaimed knowing this (e.g. "not sure"); once true it
  persists unless the model sets it false after the student volunteers.
- Keep `action ∈ {ask, done}`, `target_node_id`, `acknowledgement`,
  `question`, `public_clause_coverage` may be DELETED from the schema and
  prompt (its enforcement machinery is going away; the tally is the memory).
  Delete `reply` assembly duplication if it simplifies.

Control flow (`evaluate_and_ask`):
- DELETE the done-override: honor `action=done` regardless of node states
  (remove the `unresolved`-forces-ask logic at unified.py:867-892 and the
  forced-target fallback). If action=ask but target_node_id is
  missing/unknown, default target to the first non-understood node — for
  logging only, never to replace the model's question.
- BUDGET: if `questions_asked >= cap` before the call, skip the LLM call
  entirely and return action=done. Cap from env
  `APOLLO_UNIFIED_QUESTION_CAP` (int, default 8; add to `.env.example`).
- DELETE: `_echoes_student`; `_broad_reask_index`; the reject→retry loop and
  `_retry_feedback`; `_fallback_question`, `_narrow_generic_probe`,
  `_GENERIC_FALLBACK`, `_least_recent_canned` and the whole canned chain;
  the repeated-question BLOCK (keep a log-only counter: if the normalized
  question equals a prior Apollo question in the transcript, log
  `repeated_question_served=true` in the decision line — metric, never a
  block); `COMMON_ENGLISH_WORDS` import and the invented-vocabulary
  allowlist half of `_private_content_violations`; delete
  `apollo/smart_questions/common_words.py` if nothing else imports it.

### 3. The belt (replaces the guard) — three atom classes only

Rewrite `_private_content_violations` to flag ONLY, on the full reply
(ack + question):

1. **Digits:** any token containing a digit that appears in NEITHER the
   public problem text NOR any student message.
2. **Private vocabulary atoms:** any normalized token (≥3 chars) that
   appears in private reference content (node/edge labels + private
   strings) AND in neither public problem nor student messages AND is not
   in a small inline set of English stopwords/function words (build a
   ~200-word frozenset inline — articles, prepositions, pronouns, auxiliaries,
   common verbs like is/are/have/get/make; do NOT resurrect the 3000-word
   allowlist). This catches proper nouns (Toffler) and technical terms
   (transience) because they exist in the rubric; ordinary invented English
   ("people", "live", "day") passes because it is not in the rubric or is a
   function word.
3. **Phrase containment:** unchanged core — any normalized private string
   ≥4 chars contained in the normalized reply and not in public+student
   text (unified.py:567-575 semantics, byte-equivalent behavior).

On a belt hit: ONE regenerate call — append-only messages (preserve the
cache prefix exactly as the current retry does), feedback names ONLY the
forbidden atoms/classes, nothing else. If the regenerate hits the belt
again: serve the verbatim public clause text of the first non-understood
clause-mapped node (public text is safe by definition), log
`belt_exhausted=true`. Shape check stays: exactly one question ending in
"?"; a malformed draft gets the same single-regenerate treatment.

### 4. Prompt rewrite (`_SYSTEM_PROMPT`) — goals, codex writes wording

No problem-specific content anywhere (standing rule). Rewrite to encode:
- OBJECTIVE: maximize what the student reveals about the reference material.
  When the salient public clause is covered, OPEN an untouched (missing)
  node with a naive, curious question a confused classmate would ask, using
  ordinary words. Never emit a private atom: numbers/dates, names, or
  distinctive technical terms/phrases from the private rubric — everything
  else is sayable, including ordinary words the student hasn't used and
  faithful reuse of the student's own words.
- DELETE the closing self-check paragraph ("privately check that every
  student-facing subject-matter word came from either the public problem or
  a student message") and the over-broad "never introduce an example,
  relationship, technical term, date, name…" clause — replaced by the
  three-atom-class rule above.
- TALLY DUTY: `tally_state` is your own prior judgment — update only nodes
  the new turn changes; do not re-derive history. Set `student_declined`
  when the student explicitly says they don't know; NEVER re-ask the
  substance of a node with `student_declined=true` or status `understood`
  unless the student volunteers new information about it.
- DONE: choose done when you judge coverage is sufficient, the student
  signals done, or little productive territory remains. (No "only when
  every node understood".)
- Keep: brief-ack rule from #184, JSON discipline, exactly-one-question.

### 5. `controller.py` — persistence seam

`plan_next_question`: load tally rows for the attempt → build `tally_state`
(defaults for absent nodes) and `budget` → call `evaluate_and_ask` → apply
`tally_updates` (upsert rows; code enforces: evidence quotes must be
verbatim substrings of the referenced student turn — drop invalid updates
with a log line, keep prior state) → on a served ask, increment
`times_asked` and set `last_asked_turn` on the TARGET node. Stop reading
`ReferenceQuestionOpportunity` for questioning decisions entirely; keep
writing its rows exactly as today (audit continuity — removal is a later
cleanup). The transcript passed in stays the served text from the DB
(already true — note it in the doc).

### 6. Observability

Decision log keeps: action, target, tally counts by status, budget
`questions_asked/cap`, `fallback_reason` (now only: None |
belt_regenerated | belt_exhausted | malformed_regenerated | budget_exhausted),
`repeated_question_served`. Debug log (flag unchanged): draft, belt
verdicts + offending atoms, regenerate text/verdict, final. Remove
dead-reason plumbing.

## Tests (suite green; diff-cover ≥95 vs origin/staging)

Rewrite the smart_questions suite around the new flow (many existing tests
assert deleted machinery — rewrite or drop them WITH the machinery):
- done honored: model returns done with nodes still missing → action=done
  (the old override behavior is asserted GONE).
- Budget: questions_asked ≥ cap → done without an LLM call; below cap
  proceeds.
- Tally round-trip: updates persisted (status, evidence, declined,
  times_asked increments on target only); invalid evidence quote → update
  dropped, prior row intact, log line emitted; absent rows default missing.
- tally_state + budget serialized into the payload AFTER the stable prefix;
  key-order/cache test updated and passing.
- Belt: "1970" (digit not in public/student) blocked; "Toffler" (private
  vocab) blocked; a normalized private phrase built of common words blocked;
  "does this change anything about how people live day to day?" (invented
  ordinary English, none in rubric) PASSES; student-said tokens pass;
  faithful-paraphrase ack (4+ token overlap with student) PASSES (echo guard
  gone).
- Belt hit → exactly one regenerate (append-only messages, byte-identical
  prefix) → pass serves regenerate; second hit serves verbatim public
  clause + belt_exhausted.
- Repeated question → served + logged, never blocked.
- Prompt hygiene: no problem-specific strings; the deleted self-check
  wording absent.
- Controller: ReferenceQuestionOpportunity rows still written; no read
  drives the decision.
- Migration/model: exercised through the SQLAlchemy model in the existing
  test-DB fixtures (as ReferenceQuestionOpportunity tests do). Local only.

## Docs & git

- Reconcile `docs/architecture/apollo.md` unified-questioning + leak-guard
  sections fully (tally persistence, belt semantics + accepted residual
  risk, budget, done semantics, deleted machinery); `last_verified:
  2026-07-16`.
- NEW worktree off `origin/staging`, branch `feat/apollo-tally-questioning`.
  Do NOT touch the main checkout (feat/apollo-evidence-graph-v2-wave5,
  uncommitted work) or any existing worktree. Conventional commits — MUST
  commit (multiple logical commits welcome: migration/model, unified
  rewrite, controller, tests, docs). No push, no PR.
- Gates from the worktree (venv `ai-ta-backend/.venv`): pytest
  smart_questions + handlers chat/intent suites with coverage xml; ruff
  check (changed files) + ruff format on files you touch; mypy on
  unified.py/controller.py; `python -m diff_cover.diff_cover_tool
  coverage.xml --compare-branch=origin/staging --fail-under=95`. Report:
  worktree path, branch, SHAs, verbatim gate outputs, and any deviation
  from this spec with rationale.
