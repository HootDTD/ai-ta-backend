# Spec: Apollo unified questioning — LLM-owned clause coverage, prompt de-overfit, setup fixes

Date: 2026-07-15 · Author: styx conductor (claude) · Implementer: codex
Repo: `ai-ta-backend` · Base: `origin/staging` (head `06aff9d`, includes PR #180)

## Problem

Live transcript (Future Shock problem, 3 public clauses):

> Problem: "What is Future Shock, and why does it occur? When did it start
> happening — can you give an example? And is it still happening today — why
> or why not?"
> Student: "future shock occurs when things are happening too quickly and it
> becomes difficult to keep up"
> Apollo: "When did it start happening — can you give an example?"
> Student: "it started happening in 1970"
> Apollo: "What is Future Shock, and why does it occur?"   ← BAD: re-asks the
> clause the student already attempted, instead of advancing to clause 3.

Root cause is model drafting behavior (the transcript predates PR #180's
prompt rules). PR #180 patched it with a string-overlap guard plus
problem-specific prompt examples. Direction agreed with the user:

- **The LLM owns the judgment of which public clauses are answered /
  partially attempted / untouched.** The one-call design gives it full
  context; use it.
- The deterministic layer only **enforces the model's own judgment** when
  selecting targets/fallbacks. It must NOT grow new string heuristics.
- The system prompt must contain **no content from any specific problem or
  transcript** (overfitting + wording bias). Abstract rules only; at most one
  schematic, domain-neutral illustration.

## Out of scope (explicitly deferred)

- Any change to the existing string guards: `_broad_reask_index`,
  `_echoes_student`, `_leaks_private_content`, stemming, token overlap
  thresholds. Leave their behavior byte-for-byte as-is.
- Any DB migration. No remote deploys, no pushes, no PR.

## Changes

All in `ai-ta-backend`; primary files `apollo/smart_questions/unified.py`,
`apollo/smart_questions/controller.py`,
`apollo/smart_questions/tests/test_unified.py`, owner doc
`docs/architecture/apollo.md`.

### 1. Structured-output schema: per-clause coverage

In `_schema()`, add a required top-level `public_clause_coverage`: array of
`{ "index": integer, "status": "unattempted" | "attempted" | "answered" }`,
one entry per `public_question_parts` entry (strict mode; keep
`additionalProperties: false`). Decoding stays lenient like the rest of the
module: invalid/missing entries default to `unattempted` (i.e. current
behavior when absent).

Definitions the model works to:
- `answered` — student messages meaningfully and essentially completely
  address the clause.
- `attempted` — meaningful but partial/unclear evidence.
- `unattempted` — no meaningful student evidence.
Recomputed every turn from STUDENT messages only.

### 2. System prompt rewrite (`_SYSTEM_PROMPT`)

- Add a CLAUSE COVERAGE duty describing the field above, and rewrite TARGET
  SELECTION in its terms: never draft a question whose substance re-asks a
  clause marked `answered`; prefer `unattempted` clauses; revisit an
  `attempted` clause only with a narrower diagnostic probe — never the clause
  text itself.
- REMOVE both problem-specific passages: (a) the sentence quoting "What is
  Future Shock, and why does it occur?" and its "(for example,
  when/example/today)" tail; (b) the entire "Good pattern / Bad pattern"
  paragraph (also Future Shock–specific). Replace with abstract rules; if an
  illustration is genuinely needed, use schematic placeholders (e.g. "clause
  A / clause B"), zero real topic vocabulary.
- Leave the private-leak boundary rules and JSON discipline untouched.

### 3. Deterministic enforcement of the model's own judgment

In `evaluate_and_ask` / `_fallback_question`:
- Build a clause-status map from `public_clause_coverage`.
- If the model's `public_question_part_index` points at a clause it itself
  marked `answered`, do not use that clause for the fallback. Fallback
  preference: first `unattempted` clause (verbatim clause text OK) → else an
  `attempted` clause via `_narrow_generic_probe` (never verbatim) → else
  `_GENERIC_FALLBACK`.
- Existing string guards keep running on the drafted question exactly as
  today; this change only governs target/fallback selection.
- Extend `_log_decision` with the clause-status tally (e.g.
  `clauses={'answered': 1, 'attempted': 1, 'unattempted': 1}`) so re-ask
  frequency is measurable post-deploy.

### 4. Ledger stores the bare question, not the reply

Bug found by execution: `controller.py` stores `result.reply`
(acknowledgement + question) into `ReferenceQuestionOpportunity.question`,
but the repeat checks and fallback dedup compare bare question text — so when
turn N's reply carried an acknowledgement, the guard's fallback can re-ask
the clause the student just answered.

Fix: add `question: str | None` to `UnifiedQuestionResult` (the final
validated bare question, post-fallback). Controller persists that; `reply`
stays the full chat turn. Same column, better content — no migration.

### 5. Setup: reasoning effort default

`_DEFAULT_REASONING_EFFORT`: `"low"` → `"medium"` (clause-coverage judgment
is exactly what low effort does worst). Keep the
`APOLLO_UNIFIED_QUESTION_REASONING_EFFORT` env override. Note the
latency/cost tradeoff in the owner doc.

### 6. Tests (repo gate: diff coverage ≥95% vs origin/staging)

- Turn-2 regression mirroring the reported transcript: two student messages
  (clause 1 partial, clause 2 answered), model marks coverage
  `{0: attempted, 1: answered, 2: unattempted}` yet requests part 0 with a
  verbatim clause-1 re-ask → final reply must ask clause 3
  ("is it still happening today — why or why not?").
- Fallback redirect: requested index marked `answered` → first `unattempted`
  clause chosen; all clauses `attempted`/`answered` → narrow probe, never a
  verbatim clause.
- Controller test: with an acknowledgement present, the ledger row stores the
  bare question (not the reply).
- Prompt hygiene test: `"future shock"` (case-insensitive) does not appear in
  `_SYSTEM_PROMPT`.
- Update existing test payloads for the new required schema field; the
  existing suite must stay green.
- Gates: `pytest` (smart_questions + chat clarification suites), `ruff`,
  `mypy`, `diff-cover --compare-branch=origin/staging --fail-under=95`.

### 7. Drift contract

Update `docs/architecture/apollo.md` (owner doc) in the same commit: unified
questioning section — per-clause coverage field, fallback preference order,
ledger semantics, default reasoning effort. Bump `last_verified: 2026-07-15`.

### 8. Git mechanics (IMPORTANT)

- The main checkout of `ai-ta-backend` is on
  `feat/apollo-evidence-graph-v2-wave5` with UNCOMMITTED work. Do NOT switch
  branches or touch anything in that working tree.
- Create a fresh worktree from `origin/staging`, branch
  `fix/apollo-unified-clause-coverage`.
- Copy this spec into the branch as
  `docs/_archive/specs/2026-07-15-apollo-unified-clause-coverage-spec.md`.
- Conventional commits; the work MUST be committed (do not leave the tree
  dirty). Do not push. Do not open a PR.
- Report back: worktree path, branch, commit SHAs, and verbatim gate outputs.
