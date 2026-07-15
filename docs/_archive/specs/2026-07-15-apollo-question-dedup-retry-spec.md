# Spec: Apollo unified questioning — transcript-wide question dedup + redraft retry

Date: 2026-07-15 · Author: styx conductor (claude) · Implementer: codex
Repo: `ai-ta-backend` · Base: `origin/staging` (must include PR #181, merged 19:49Z)
Prior spec: `docs/_archive/specs/2026-07-15-apollo-unified-clause-coverage-spec.md`

## Problem (evidence from staging logs, deploy a04dae81, 19:55–19:59Z)

Live session on the post-#181 build. Decision logs show `fallback_reason=
question_vocabulary_boundary` on 4 consecutive turns (plus one
`unsafe_acknowledgement`): **nearly every LLM-drafted question was rejected by
`_leaks_private_content`**, so the student was served the canned fallback
chain. Two failures resulted:

1. **A verbatim repeat.** "What makes that happen?" was asked at turn N and
   again at turn N+2. Mechanism: question dedup uses `prior_questions` from
   the per-node ledger (`ReferenceQuestionOpportunity`), which keeps only the
   LATEST question per node. When a row is overwritten, the system forgets it
   ever asked the earlier question, and the canned probe becomes "fresh"
   again. For this problem the canned repertoire is effectively 2 probe
   strings + `_GENERIC_FALLBACK` (clauses 1 and 3 both contain "why" → the
   same probe), so exhaustion + ledger amnesia → repeats.
2. **Robotic voice.** With drafts rejected almost every turn, Apollo speaks
   mostly in hardcoded strings, defeating the purpose of the unified LLM call.

User rule to implement: **Apollo must never ask the same question twice in an
attempt** — enforced deterministically (the LLM is not the one repeating; its
output is being discarded).

## Out of scope

- No loosening/tightening of the leak guard's token rules themselves
  (`_leaks_private_content` internals stay as-is) beyond item 3 below, which
  only ADDS already-exposed vocabulary to the safe set for QUESTIONS.
- No change to `_broad_reask_index` / `_echoes_student` heuristics.
- No DB migration.

## Changes (all in `apollo/smart_questions/unified.py` unless noted)

### 1. Transcript-wide question dedup (the user's rule)

In `evaluate_and_ask`, derive the full set of questions Apollo has already
asked this attempt from the `transcript` parameter (role == "apollo"
messages; extract the question — the message text from the last sentence
boundary before the terminal "?" or, simpler and acceptable, the whole
normalized message plus, when present, the substring from the previous
sentence end to the trailing "?"). Union it with the ledger-derived
`prior_questions`. Use this combined set for BOTH the `repeated_question`
check and every `seen` dedup inside `_fallback_question`. The ledger stays
as-is (it serves per-node state); the transcript becomes the non-lossy
dedup memory. No schema or DB change.

### 2. One redraft retry before canned fallback

When the drafted question or acknowledgement is rejected
(`question_vocabulary_boundary`, `question_echo`, `repeated_question`,
`broad_reask_after_evidence`, `malformed_question`, `unsafe_acknowledgement`),
make ONE follow-up call to the model before falling back to canned strings:

- Same schema; append to the original messages the assistant's own prior JSON
  output and a user message stating the rejection reason and the constraint
  violated. For vocabulary rejections, include the specific offending tokens
  (factor `_leaks_private_content` minimally to return them — internals
  unchanged, just surface what it found). For repeats, include the list of
  already-asked questions and say a NEW question is required.
- Re-validate the redraft with the full guard set. If it passes, use it.
  If not, fall back to the canned chain exactly as today.
- Exactly one retry per turn (bounded latency; the extra call happens only on
  rejected turns). Precedent: PR #175 did this for the old writer chain.

### 3. Leak-safe vocabulary widening for QUESTIONS only

Add the tokens of prior APOLLO messages in the transcript to the safe-token
set used when validating the drafted QUESTION (not the acknowledgement —
its stricter student-words-only boundary stays). Rationale: anything Apollo
already said passed the guard when it was first said and is already exposed
to this student; refusing continuity wording ("you mentioned phones…" style)
adds rejection churn with zero leak-prevention value. The private-strings
containment check still runs unchanged on the full reply.

### 4. Canned-chain last resort (no consecutive repeats, ever)

If after dedup the canned chain has nothing unseen (all clause texts and both
probe strings already asked) choose the least-recently-asked canned option
that is NOT the immediately preceding Apollo question; only if the repertoire
has a single member left may it repeat, and never twice in a row.
Deterministic and simple — with item 2 in place this path should be rare.

### 5. Observability

`fallback_reason` must reflect the retry: e.g. rejection reason suffixed with
`_retry_recovered` when the redraft passed, `_retry_failed` when the canned
chain took over. Keep the clause tally logging from #181.

## Tests (diff coverage ≥95% vs origin/staging)

- Regression mirroring the live session: canned probe asked at turn N,
  ledger row later overwritten, model draft rejected again → the probe must
  NOT be repeated (transcript dedup catches it).
- Retry: first draft trips vocabulary guard → redraft (mocked second call)
  passes → redraft is served; second rejection → canned fallback; exactly
  two model calls total.
- Retry on repeated_question includes the asked-questions list in the
  feedback message.
- Apollo-message vocabulary accepted in a QUESTION draft but still rejected
  in an ACK draft.
- Last-resort: fully exhausted repertoire never yields the same question
  twice consecutively.
- Update existing tests for the new call flow; whole suite green.
- Gates: pytest (smart_questions + chat clarification), ruff, mypy,
  diff-cover ≥95 vs origin/staging.

## Docs & git

- Reconcile `docs/architecture/apollo.md` (same commit), `last_verified`
  2026-07-15.
- Copy this spec into the branch as
  `docs/_archive/specs/2026-07-15-apollo-question-dedup-retry-spec.md`.
- NEW worktree off origin/staging (do NOT touch the main checkout on
  feat/apollo-evidence-graph-v2-wave5), branch
  `fix/apollo-question-dedup-retry`. Commit (conventional), no push, no PR.
- Report: worktree path, branch, SHAs, verbatim gate outputs, deviations.

---

## ADDENDUM (2026-07-15, user request — apply on top of the branch)

### A. Flag-gated diagnostic logging of the draft→reject→redraft cycle

Goal: collect evidence on guard precision so the rejector can eventually be
relaxed with data instead of guesses.

- New env flag `APOLLO_UNIFIED_QUESTION_DEBUG_LOG` (default OFF, add to
  `.env.example`). When on, emit ONE additional structured log line per turn:
  raw drafted acknowledgement + question, every rejection reason with its
  offending tokens, the redraft text and its validation outcome, the final
  served question, target node id, clause statuses. Truncate each free-text
  field to ~300 chars.
- Default-off is deliberate: rejected drafts may contain private-rubric
  content, and the standing logging contract (apollo.md) promises no
  transcript/private content in logs. The flag knowingly suspends that for
  staging diagnosis; prod keeps it off. Update the apollo.md logging
  paragraph to document the flag and this boundary.
- This is in addition to (not instead of) the aggregate
  `fallback_reason` retry suffixes in item 5.

### B. Prompt-cache-friendly call structure

OpenAI prompt caching is automatic for gpt-5.x on shared prefixes (≥1024
tokens) — nothing to enable. Note for the doc: caching affects ONLY speed and
cost; it cannot change model outputs or error rates (same tokens, same
computation, precomputed). The requirements are purely structural so the
automatic cache actually hits:

1. The retry call MUST be append-only on the first call's messages: reuse the
   exact same serialized system prompt and payload string (no
   re-serialization), then append the assistant's first raw JSON output and
   the rejection-feedback user message. Byte-identical prefix → the retry
   reads the first call's tokens from cache and pays mostly for the appended
   feedback + completion.
2. Keep the payload key order stable with per-attempt-stable fields first
   (`public_problem`, `public_question_parts`, `private_reference_nodes`,
   `private_reference_edges`) and per-turn-varying fields last
   (`question_history`, `transcript`) — already true today; add a brief code
   comment and a test asserting the key order so future edits don't silently
   destroy cross-turn cache hits.

### Addendum tests

- Debug log line emitted when the flag is on; absent when off.
- Retry call's messages array == first call's messages + exactly 2 appended
  entries, with the first two entries byte-identical to the first call.
- Payload key-order regression test.

Same gates as the main spec; new commit on the same branch (do not amend).
