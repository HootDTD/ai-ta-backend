---
doc: apollo/overseer/jev-node-judge
description: EXPERIMENTAL per-turn node judge on TypeSafe Jev (APOLLO_NODE_JUDGE=jev, default off) + its HTTP client; emits the same coverage verdict as the GPT adjudicator.
owns:
  - apollo/overseer/jev_node_judge.py
  - apollo/overseer/jev_client.py
related:
  - apollo/overseer/transcript-coverage
  - apollo/overseer/topic-score
  - apollo/conversation/handlers/done
  - platform/config-settings
last_verified: 2026-10-29
stub: false
---

# Overseer Jev node judge — per-turn coverage on a decision model

An ALTERNATIVE node-coverage judge for the Done path, selected only by
`APOLLO_NODE_JUDGE=jev` (default `gpt`; see [config-settings](../../platform/config-settings.md)).
It is built for the pre-registered benchmark in
`.planning/specs/2026-10-29-apollo-jev-node-judge-design.md` (workspace, not this repo) and is
**not** the grader of record — [transcript-coverage](transcript-coverage.md) still is. Unset, the
Done path never sends a request to Jev and the grade is byte-identical.

## Interface

`jev_node_judge.py` (names pinned — the benchmark harness imports them):

- `LABELS = ("covered", "partial", "not_covered", "incorrect")`; `LABEL_CREDIT` = 1.0 / 0.6 / 0 / 0;
  `PARTIAL_CREDIT = 0.6`; `LABEL_RANK` (`covered > partial > incorrect > not_covered`).
- `JEV_INSTRUCTIONS_TEMPLATE` (format keys `node_type`, `node_text`) and `JEV_CRITERIA`
  (one sentence per label) — the exact wording Jev sees, so a run can record what was asked.
- `@dataclass TurnNodeCell(turn_index, node_id, label, probabilities, confidence, latency_ms)`,
  `@dataclass JevJudgeResult(verdict, cells, winning_turn, usage)`.
- `async judge_transcript(reference_graph, transcript_messages, *, client=None) -> JevJudgeResult`.
  Raises on any failure (no fallback anywhere).
- Builders: `build_question(item)`, `build_question_payload(rubric_items)` (qids `n0..nK` in
  rubric order), `question_ids(rubric_items)` (`{qid: node_id}`), `build_state(turn)`,
  `student_turns(transcript_messages)`, `node_text(item)`; pure `aggregate(node_ids, cells)`;
  `evidence_spans(result, transcript_messages)`.

`jev_client.py`:

- `JevClient(*, api_key=None, endpoint, model="jev-latest", timeout=15s, transport=None, sleep,
  jitter).evaluate(state, questions) -> JevResponse(model, answers{qid: JevAnswer(choice,
  confidence, probabilities)}, usage, latency_ms)`. `api_key` defaults to `JEV_API_KEY`
  (`config.settings.get_jev_api_key`); `transport` / `sleep` / `jitter` are test seams.
- `JevError(message, status_code=None)` — the ONLY exception type it raises.

## Data flow

`done.py` passes the same aside-filtered `(role, content)` transcript it hands the GPT
adjudicator. `student_turns` walks it: each `student` row becomes one turn with `state =
{apollo_question: the latest preceding apollo row | null, student_answer, previous_student_answer:
prior student row | null}`. Rubric = `transcript_coverage._build_rubric_items` (graded node types,
same `display_name` + `content` the GPT lane sees); each item becomes one `choice` question whose
instructions end `Reference idea (<type>): <display_name> — <content json>`. One
`POST /v1/systemone` per turn (≤ `MAX_CONCURRENT_TURNS` = 8 in flight) carries every node's
question; Jev answers them in parallel.

`aggregate` then, per node: best label across turns by `LABEL_RANK`, ties to the EARLIEST turn →
`procedure_scores` = `LABEL_CREDIT`, `per_step` covered at credit ≥ 0.5 (so `partial` is
`covered`, same threshold as the GPT lane), `confidences` = winning cell's confidence, `basis` =
winning label, zeroed `negotiation_counts`; `validate_coverage_verdict` before return.
`evidence_spans` maps each credited node to its winning student turn VERBATIM (one whole message,
so it passes `validate_span`) and feeds the narrative like the GPT spans.

## Invariants & gotchas

- **No fallback (local experiment).** Any exception from `judge_transcript` (missing key, HTTP,
  timeout, malformed body, an absent answer, an off-label choice) propagates out of
  `done._jev_coverage` and fails the Done. The flag is for local benchmarking only and is never
  set on a deployed environment.
- **Retry only 429/529**, backoff 1s/2s/4s each stretched by ≤25% jitter, then raise. 401/422/any
  other non-200 and timeouts raise at once. `latency_ms` is the whole call incl. backoff. The key
  is never logged.
- **`basis` carries the Jev label**, a vocabulary of its own (`coverage_contract.JEV_BASIS_VALUES`)
  accepted alongside the adjudicator's four; `incorrect` (credit 0) is kept only when no turn
  reached `partial` or better, so feedback can say "wrong" vs "not mentioned".
- **No `hoot_assisted` / `wrongness` keys.** Jev answers neither question, so under `jev` the
  INTERACTION5 cap and P3.2 corroboration read as "nothing assisted / not corroborated", and the
  `course_evidence` / `tally_context` prompt frames are not used. Both lanes default OFF.
- **Zero student turns → every graded node `not_covered`, confidence 1.0, `winning_turn` None**,
  with no request sent (Done refuses empty attempts upstream anyway).
- **Each call sees one turn.** An idea assembled across turns may be under-credited — a design
  property the benchmark measures, not a bug to patch here. Mapping-shaped transcript rows (e.g.
  exports) are accepted too; rows with `intent == ASIDE_MESSAGE_INTENT_TAG` are dropped.
- **Limits:** 64k tokens/request, state + longest question ≤ 32k; an oversize turn is a 4xx →
  fallback. Lowercase `type` only.
- `httpx` is used directly; it is already installed as an `openai` dependency (and pinned in
  `requirements-test.txt`), not a new package.
