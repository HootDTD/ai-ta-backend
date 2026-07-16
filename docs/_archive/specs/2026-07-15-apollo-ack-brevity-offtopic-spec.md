# Spec: Apollo questioning — brief acks, respect "I don't know", stop off_topic hijack

Date: 2026-07-15 · Author: styx conductor (claude) · Implementer: codex
Repo: `ai-ta-backend` · Base: `origin/staging` (head includes PR #183 merge
`366e70e`). NEW worktree, branch `fix/apollo-ack-brevity-offtopic`.

## Evidence (staging session 83, post-#183 deploy, 23:59–00:08Z)

1. Every drafted acknowledgement was rejected `unsafe_acknowledgement` with
   EMPTY offending tokens — vocabulary passed; the killer is
   `_echoes_student` (any 4-token run shared with a student message). The
   model drafts long restatements ("So far I've got that Future Shock is
   when change happens too quickly and it becomes difficult to keep up") that
   inevitably share 4-grams with the student's own sentence. The retry
   feedback ("may use student words only") then steers the redraft into a
   VERBATIM student echo — guaranteed rejection. Result: bare questions,
   robotic voice.
2. Turn 00:08:57 (`draft_rejection=None` — guards not involved): the model
   re-asked for an example from the era the student had already said
   "im not sure what is an example" about. No prompt rule covers explicit
   student uncertainty.
3. Student's "noy sure" (typo of "not sure", a direct answer to the pending
   question) was classified `off_topic` ≥0.7 by the cheap intent classifier
   and the turn was hijacked with the canned dead-end "I noticed you're
   talking about something else…" — it never reached the unified questioner.

## Direction decided with the user

- Fix 1 and 2 at the PROMPT level. The leak/echo guards themselves stay
  byte-identical — no relaxation, no new heuristics.
- This spec states GOALS for the prompt changes; codex writes the actual
  wording. Standing rule: the system prompt must contain NO
  problem-specific or transcript-specific content — abstract rules only.
- Fix 3 structurally: `off_topic` stops hijacking, period.

## Changes

### 1. Prompt goal: brief acknowledgements (`_SYSTEM_PROMPT`, unified.py)

Goal to encode (wording is codex's): the optional acknowledgement must be a
SHORT connective phrase — brief signal that the student was heard — never a
multi-clause restatement or summary of what the student said. Long faithful
restatements are doubly wrong: they sound like meeting minutes, and the
echo guard (which stays as-is) structurally rejects any 4-token run shared
with student messages, so they never survive anyway. A brief ack passes the
guard naturally. Keep the existing boundary rule (ack may assert only
information already present in student messages, no question mark) intact
alongside the new brevity goal.

Also update the `unsafe_acknowledgement` constraint string in
`_retry_feedback` (unified.py:773-775): it currently says "The
acknowledgement may use student words only…", which actively steers
redrafts into verbatim echo (observed in the debug log). Align it with the
brevity goal: keep it short, don't restate the student's sentences, no
question mark. Same goal-not-wording rule applies.

### 2. Prompt goal: respect explicit uncertainty (`_SYSTEM_PROMPT`)

Goal to encode (wording is codex's): when the student has explicitly
signalled they don't know / can't recall something Apollo asked, Apollo
must not re-ask that same substance again — not even reworded. It should
either probe a genuinely different aspect or clause, or, when nothing
productive remains to ask, choose `action=done` so the flow moves
gracefully toward grading (the existing done handoff covers the "no
worries, let's go to grading" experience). Abstract rule only; no
deterministic guard is added for this — the LLM owns the judgment.

### 3. Stop the off_topic hijack (`apollo/handlers/chat.py`, `intent.py`)

- In `_maybe_intent_confirmation` (chat.py:~283-295): when the verdict is
  `off_topic`, return None — fall through to the teaching path regardless
  of confidence. The unified questioner has full context and handles
  redirection better than a canned dead-end.
- Emit one INFO log line on that fall-through (e.g. intent + confidence) so
  we can still see how often the classifier would have hijacked. No
  transcript content in the log line (standing logging contract).
- Remove the now-dead `off_topic` entry from `confirmation_prompt_for`
  (intent.py:187). Keep `off_topic` in the classifier taxonomy/prompt —
  only its gate action is removed.
- `done`, `restart`, `next`, `return_to_hoot`, `help` behavior unchanged.

### 4. Minor observability (debug log only)

In the `apollo_unified_question_debug` line, when an acknowledgement is
rejected, include WHICH ack sub-check fired (question-mark / vocabulary /
echo) and, for vocabulary, the offending tokens (currently the tokens field
only reflects the question check, which is why this session's lines were
uninformative). Flag-gated debug line only; the aggregate decision log is
unchanged.

## Tests (suite green; diff-cover ≥95 vs origin/staging)

- Off_topic regression: classifier verdict off_topic at high confidence →
  no confirmation turn persisted, `pending_intent` not set, turn reaches
  the teaching path. Update/remove existing tests asserting the old hijack.
- Other intents still hijack (e.g. done above threshold unchanged).
- Retry feedback for unsafe_acknowledgement no longer instructs
  student-words-only echoing (assert on the new constraint's intent, keep
  the assertion loose enough to survive wording tweaks).
- Ack sub-cause visible in the debug line when the flag is on.
- Prompt hygiene tests still pass (no problem-specific vocabulary).

## Docs & git

- Reconcile `docs/architecture/apollo.md` in the same commit: ack brevity
  goal, uncertainty rule, intent gate no longer hijacks off_topic;
  `last_verified: 2026-07-15`.
- Copy this spec into the branch as
  `docs/_archive/specs/2026-07-15-apollo-ack-brevity-offtopic-spec.md`.
- NEW worktree off `origin/staging` (do NOT touch the main checkout on
  `feat/apollo-evidence-graph-v2-wave5`, and do NOT reuse the merged
  `fix/apollo-question-dedup-retry` worktree/branch). Branch
  `fix/apollo-ack-brevity-offtopic`. Conventional commit(s); MUST commit;
  no push, no PR (conductor reviews, pushes, opens the PR).
- Gates: pytest (smart_questions + chat/intent suites), ruff, mypy,
  `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`.
  Report worktree path, branch, SHAs, verbatim gate outputs.
