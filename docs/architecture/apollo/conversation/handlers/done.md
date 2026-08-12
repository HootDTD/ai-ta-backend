---
doc: apollo/conversation/handlers/done
description: apollo/handlers/done.py — handle_done, the grade-of-record orchestrator that assembles the whole Apollo grading path
owns:
  - apollo/handlers/done.py
related:
  - apollo/conversation/routing/errors
  - apollo/overseer/_index
  - apollo/overseer/transcript-coverage
  - apollo/overseer/rubric
  - apollo/overseer/topic-score
  - apollo/overseer/diagnostic
  - apollo/overseer/grounding
  - apollo/overseer/aside-penalty
  - apollo/overseer/xp
  - apollo/conversation/handlers/grading-artifact-writer
  - apollo/conversation/hoot-bridge-reference-answer
  - apollo/projections/scorecard
  - apollo/projections/mastery
  - apollo/persistence/done-write-linkage
  - apollo/persistence/progress-repo
  - apollo/schemas/problem
last_verified: 2026-08-12
stub: false
---

# handlers/done — the grade-of-record ORCHESTRATOR

`handle_done` is `POST /apollo/sessions/{id}/done`. It **assembles the whole
grade path** — the grading-path recipe (D21) starts here. Cross-cutting grading
invariants (grading-lane, misconceptions-empty, composite-retired,
score→letter→narrative) live in `overseer/_index`, not restated here.

## Interface

- `handle_done(*, db, neo, session_id, auto_done=False) -> dict` — the only
  public entry (called by `routing/router`, and by `handlers/chat` when the
  intent/questioning gate decides "done"; the questioning gate passes
  `auto_done=True`). Returns the student grade payload (`rubric`, `topics`,
  `progress`, `scorecard`, `grading_provenance`, `transcript`, …).
- **M1 (P3.4) claim primitives** (wired into `handle_done`; see Invariants
  "Claim lifecycle"): `_CLAIM_PHASE`, `_STALE_CLAIM_AFTER`,
  `_claim_grading_slot`, `_release_grading_claim`, `_fence_grade_commit`,
  `_progress_block`, `_stored_grade_payload`.

## Data flow

Ordered grade assembly (each step delegates to the owner doc):

1. Load session + problem (`_find_problem`) + latest `ProblemAttempt`.
   **Empty-attempt guard** (2026-08-07, defect I1): zero persisted student
   messages (`_student_message_count`) → `EmptyAttemptError` (409 `empty_attempt`,
   `routing/errors`) BEFORE any mutation — no freeze/phase change/XP/narrative,
   attempt row untouched (marking it flips `is_reattempt_in_session` and docks XP
   on the real Done). An already-graded attempt short-circuits to its stored
   report; otherwise Done CASes the claim (Invariants "Claim lifecycle") then
   reads the graph (degraded Neo4j tolerated).
2. Derive the reference graph via `Problem.to_kg_graph` (`schemas/problem`).
2a. **Question ledger** (`_question_ledger`, P1.2b/P1.3): ONE read of this
   attempt's `QuestionOpportunity` rows (by `id`), feeding the adjudicator's
   `tally_context` (step 3 — `[{node_id, state, times_asked,
   student_quote|null}]`) AND the scorer's `asked_node_ids` (step 5) via
   `_probed_node_ids(rows)` — NOT the raw row set, since a degenerate
   `fallback_served` turn mints a row without probing anything. Any exception
   logs and yields `None` for both (pre-fix grade); an EMPTY ledger stays
   `frozenset()`.
3. **Transcript coverage** (the sole grader): `compute_transcript_coverage_with_spans`
   (`overseer/transcript-coverage`) over `_full_transcript` → coverage +
   validated evidence spans. `_course_evidence_safe` (before it) checks
   `INTERACTION2` + `INTERACTION_CONCEPTS`, then renders `grounding_bundle`
   into the optional `course_evidence` block (also step 6). `_full_transcript`
   excludes `TutoringMessage` tagged `intent == ASIDE_MESSAGE_INTENT_TAG`
   (`hoot-bridge-reference-answer`) — INTERACTION4 hint-lane text never enters
   grading, but the student's untagged triggering question does.
3a. **Hoot-assist cap** (INTERACTION5, `overseer/aside-penalty`): gated on
   `interaction5_enabled()` AND `interaction_allowed_for_concept`, `_aside_texts`
   fetches the same aside rows `_full_transcript` EXCLUDES (its exact complement)
   and passes them to the adjudicator as `hoot_asides`; `apply_aside_caps`
   (cap 0.5) then flat-caps every flagged node in the coverage BEFORE rubric /
   topic-score / diagnostic / artifacts, so all consumers see the same values.
4. `compute_rubric` (`overseer/rubric`) maps coverage into the axis rubric.
5. **Topic score** (`_compute_topic_score_safe` wrapping `compute_topic_score` /
   `compute_centrality`, `overseer/topic-score`): best-effort, computed always,
   and given `asked_node_ids` (step 2a) so never-engaged graded nodes leave the
   denominator (P1.2b). On success `served_rubric` REPLACES `overall` with the
   topic score/letter (new dict; `rubric` itself is never mutated).
6. `generate_diagnostic` (`overseer/diagnostic`) — grounded narrative plus,
   on topic-score JSON success, structured per-topic feedback from the
   student's verbatim utterances. Handed `graded_topics_only(topic_score)`,
   NOT the full result (an `unprobed` topic is excluded from the grade, so
   narrating it as a gap would contradict served `topics[]`, P2.1/U2); the
   remediation pass (`INTERACTION3`, ≤3 weak topics, citation-only) shares
   that view. Runs via `asyncio.to_thread` so the narrative LLM never blocks
   the event loop.
7. XP: `compute_xp_earned`/`compute_progress_envelope`/`apply_xp`
   (`overseer/xp` + `persistence/progress-repo`); reattempt detection via
   `has_prior_graded_attempt` (`persistence/done-write-linkage`).
8. Persist canonical artifact `write_artifacts` (`grading-artifact-writer`) →
   then project mastery `_project_mastery` → `update_mastery_from_artifact`
   (`projections/mastery`), and render `render_scorecard` (`projections/scorecard`).

## Invariants & gotchas

- **The transcript adjudicator is THE only grading lane.** A `CoverageGradingError`
  propagates to the retryable 503 handler — never a fallback to an empty-graph or
  legacy grade. A degraded KG never yields a false F (grading reads the transcript).
- **`_compute_topic_score_safe` is soft-fail**: any exception → `topic_score=None`,
  `served_rubric is rubric` (byte-identical), and `topics` is absent (not null).
- **Structured feedback is additive and topic-only:** successful topic JSON is
  served as `student_response["feedback"]`; parse/LLM failure or no topic score
  leaves that key absent. `diagnostic_narrative` always remains the string
  back-compat surface (flattened structured output on success, legacy output on
  soft-fail).
- **Remediation cannot affect grading:** one `try/except` encloses the complete
  copy-on-success pass. Any retrieval/shape failure (or a non-matching concept)
  leaves feedback byte-identical with no `review` keys, and score, letter,
  narrative, XP and persistence unchanged. A non-null Interaction-1 bundle
  prevents fresh retrieval.
- **Artifact write + artifact-derived mastery are own-failure-domain telemetry** —
  each owns its commit and swallows exceptions; neither voids the served grade.
  `_project_mastery` is skipped when `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is on.
- **Course grounding never adds a failure mode** (`overseer/grounding`):
  `_course_evidence_safe` runs AHEAD of the grading lane and is soft-fail by
  construction — flag off, concept disallowed, corrupt bundle, or ANY
  exception → `None` (both prompts byte-identical to pre-feature). Additive
  `grading_provenance["grounding"]` is the replay-diff hook.
- **The Hoot-assist cap owns its failure domain** (`overseer/aside-penalty`):
  the aside fetch and `apply_aside_caps` are wrapped so ANY exception logs and
  leaves `coverage` UNCAPPED (never half-caps); runs AHEAD of the sole grading
  lane, never touches the `CoverageGradingError → 503` contract, and can only
  lower a grade. Additive `grading_provenance["aside_penalty"] = {enabled,
  cap: 0.5, assisted_node_ids}` when the gate fired; off → key absent.
- The persisted `attempt.diagnostic_report` stores `{narrative, rubric (RAW),
  coverage, served_overall}` plus two conditional keys, each absent when it
  does not apply: `auto_done: true` iff the questioning engine (not the
  student) triggered this Done (P0.4), and `unprobed_node_ids` — nodes
  P1.2b dropped from THIS grade, read by `projections/performance-problems`
  so its node drill-down never re-derives a class-wide "missed" from
  `coverage` alone. `served_overall` snapshots `served_rubric["overall"]`;
  re-serving surfaces read it first, falling back to `rubric.overall` for
  pre-snapshot rows.
- The response keeps historical `graph_lane: null` for API compatibility.
- **Does NOT import `done_turn_order`** (the WU-4C1 shadow chain — A7 removed it).
- **`grading_provenance.reference_question_asides_used`** (additive) reads
  `sess.metadata_[ASIDE_COUNT_SESSION_METADATA_KEY]`, defaulting to 0 — never
  affects the score, just teacher-facing provenance.
- **Claim lifecycle (M1, P3.4; gated in `test_apollo_done_claim_postgres.py`):**
  `_claim_grading_slot` CASes `phase` (`IS DISTINCT FROM 'SOLVING' OR
  updated_at < now-15min`, NULL-safe) as Done's FIRST Postgres write,
  replacing the blind phase write + `store.freeze`; `_release_grading_claim`
  guards on `phase = 'SOLVING'` (stale-reclaim `prior_phase` falls back to
  `TEACHING`). **The terminal `phase='REPORT'` write is ALSO fenced**
  (`_fence_grade_commit`, same guard, P3.4 delta): a reclaimed-out Done
  writes NOTHING and raises `GradingInProgressError`. `_stored_grade_payload`
  replays with NO side effects (no XP write, no upsert/commit).

## Env flags

- `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (`_graph_sim_layer3_enabled`) — gates the
  mastery-projection interlock; default OFF everywhere.
- `INTERACTION2` (`config.settings.interaction2_enabled`) — gates course
  grounding of both prompts; default OFF, independent of `INTERACTION1` (which
  gates only whether the bundle is BUILT). Read ONLY here.
- `INTERACTION3` — weak-topic remediation citations; default OFF.
- `INTERACTION5` (`config.settings.interaction5_enabled`) — Hoot-assist grading
  cap; default OFF. Combined here with `interaction_allowed_for_concept`.
- `INTERACTION_CONCEPTS` (`config.settings.interaction_allowed_for_concept`) —
  optional concept-slug scope for course grounding and the Hoot-assist cap.

## Related

See `overseer/_index` for the grading-path cross-cutting invariants and the full
directional chain: `transcript-coverage ↔ rubric ↔ topic-score ↔ done ↔
grading-artifact-writer ↔ scorecard ↔ mastery`. Aside tagging/cap:
`hoot-bridge-reference-answer`.
