---
doc: apollo/conversation/questioning/challenge
description: The P3.2 done-gate — Apollo may not self-declare done while a graded node is owed a challenge; pure, deterministic, no extra LLM call.
owns:
  - apollo/smart_questions/challenge.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/questioning/controller
  - apollo/conversation/questioning/selection
  - apollo/conversation/questioning/leakage
  - apollo/overseer/wrongness
  - apollo/conversation/handlers/done
last_verified: 2026-08-12
stub: false
---

`apollo/smart_questions/challenge.py` decides whether Apollo **owes the student
one more question** before it is allowed to finish. It exists because the
pedagogically correct place to spend the P3.2 wrongness signal is inside the
attempt, while the student can still fix the claim — at Done every lever is a
punishment, and best-grade-wins retries defeat an at-Done penalty anyway.

Split out of `unified.py` rather than added to it: `unified` owns control flow,
this module owns the gate's decision and its rendering, and the split keeps
`unified` near the 800-line convention (the same reason `questioning/prompts`
exists).

## Interface

- `resolve(*, armed, self_declared_done, policy, tally_state, updates, transcript,
  contested_quotes, reference_graph, problem_text, student_messages,
  questions_asked, cap) -> ServedChallenge | None` — the ONE entry point, called
  from `unified.evaluate_and_ask`. `None` means serve the turn as decided.
- `ServedChallenge` (`node_id`, `reply`, `reason`, `spent`, `quoted`),
  `OwedChallenge`, `NodeFacts`, `OwedReason`.
- `owed_challenge(nodes, *, student_turns)` and `render(quote)` — the pure halves,
  exercised directly by tests.
- `challenge_budget()`, `challenges_spent(transcript)`, `clean_quote(value)` —
  the last is also the L2c quote hygiene `questioning/controller` applies to a
  carried prior quote.
- `CHALLENGE_MARKER` / `CHALLENGE_PREFIX` / `CHALLENGE_TEMPLATE` /
  `CHALLENGE_FALLBACK`, `DEFAULT_CHALLENGE_BUDGET`, `MAX_QUOTE_CHARS`.
- `TallyStateLike` / `TallyUpdateLike` / `EvidenceLike` protocols — `unified`'s
  value objects are read structurally, so this module never imports `unified`
  (that would be a cycle); `SelectionPolicy` IS imported from
  `questioning/selection`.

## Data flow

`unified` calls `resolve` immediately after `_decode_updates` and the policy
re-resolve, BEFORE the `action == "done"` early return. `resolve` checks all four
guards, builds one `NodeFacts` per GRADED node (askable ones first, in the
policy's contested-first order; the rest after), and asks `owed_challenge`:

- **(a) contradiction** — the node is askable, still under `MAX_ASKS_PER_NODE`,
  and its latest wrongness is `contradicts_material`. The quote comes from
  `contested_quotes` (the controller's `wrongness.candidate_quotes` read of the
  ledger) **aged forward by this turn's updates**: an update that appends a new
  evidence entry becomes the node's latest, so a fresh `contradicts_material`
  adds the node and any other labelled entry retires it.
- **(b) unprobed claim** — the node's effective state is `understood`,
  `times_asked == 0`, and the attempt has <= 1 student turn. The paragraph dump.

(a) outranks (b). The owed node's quote goes through `render`, then
`leakage.belt_verdict`; a leak OR a malformed shape (a student quote carrying its
own `?`) serves `CHALLENGE_FALLBACK` instead. `unified` returns the result as an
ordinary `ask` with `fallback_served=False`, so `controller` charges the ask
normally.

## Invariants & gotchas

- **Student-initiated Done is NEVER blocked, structurally.** `POST /done` and
  `chat._handle_pending_done` both call `handlers/done::handle_done` directly and
  never reach `plan_next_question`. P0.4 consent outranks anti-gaming; P3.4's
  claim/409 path is untouched. Asserted over the real call graph in
  `test_done_gate_consent.py`, with a positive control so the scanner cannot pass
  by finding nothing.
- **Only the MODEL's own `done` is overridable.** An emptied `askable_ids` is the
  CODE-enforced done and is never challenged — `self_declared_done` is a separate
  argument from the policy for exactly that reason.
- **Unreachable on the `budget_exhausted` branch**, which returns at the top of
  `evaluate_and_ask` before the decode this gate rides on. Never depend on that
  branch: it also discards the turn's tally updates (see `questioning/unified`).
- **Hard cap `APOLLO_CHALLENGE_BUDGET` (default 2), counted from the transcript.**
  No column, no migration, and it resets correctly on P0.2's restart wipe, which
  deletes the attempt's `TutoringMessage` rows. **The counted token is
  `CHALLENGE_MARKER`, not the longer `CHALLENGE_PREFIX`** — the fallback rendering
  has no "you said" clause, so counting the prefix would leave every fallback
  challenge uncounted and let the gate fire past its budget. `0` is a valid budget
  and disables the gate without touching the ladder level.
- **Never past `question_cap()`** — `questions_asked < cap` is checked here too,
  not only by the caller's early return.
- **Shape (b) is `state`-based on purpose.** B specified `credit >= 0.85`, which
  does not exist per-turn before P3.1 (crit-A A3); the substitution costs 16
  attempts gated vs 18. It also deliberately does NOT require `askable`: an
  `understood` node is excluded from `askable_ids` precisely because it is
  understood, which is the state this shape exists to challenge. The unspent-ask
  invariant is carried by `times_asked == 0` instead.
- **Ungraded nodes are never owed.** Only graded nodes carry grade risk, and
  widening the graded denominator is P1.4's decision, not this one.
- **No extra LLM call, no added latency.** The template is code-emitted and
  deterministic; a false positive costs the student exactly one question.
- **Three shared literals are duplicated here** to keep the module import-light:
  `_MATERIAL`, `_UNDERSTOOD`, and the marker strings. The first two are pinned
  against `overseer/wrongness` and `questioning/selection` by
  `test_the_gates_string_literals_agree_with_their_authorities` — a silent
  divergence would degrade to a gate that never fires, which is exactly the
  failure nobody notices.

## Env flags

`APOLLO_CHALLENGE_BUDGET` (default 2, clamped >= 0). The gate itself is armed by
`APOLLO_WRONGNESS_LEVEL >= 2`, read only through
`overseer/wrongness::effective_wrongness_level` in `questioning/controller`.

## Related

Caller and control flow `questioning/unified`; level read + `contested_quotes`
`questioning/controller`; askable set `questioning/selection`; belt
`questioning/leakage`; the predicate and the ladder `overseer/wrongness`; the
grade path the gate must never touch `handlers/done`.
