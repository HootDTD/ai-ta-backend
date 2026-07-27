---
doc: apollo/conversation/handlers/done
description: apollo/handlers/done.py — handle_done, the grade-of-record orchestrator that assembles the whole Apollo grading path
owns:
  - apollo/handlers/done.py
related:
  - apollo/overseer/_index
  - apollo/overseer/transcript-coverage
  - apollo/overseer/rubric
  - apollo/overseer/topic-score
  - apollo/overseer/diagnostic
  - apollo/overseer/xp
  - apollo/conversation/handlers/grading-artifact-writer
  - apollo/projections/scorecard
  - apollo/projections/mastery
  - apollo/persistence/done-write-linkage
  - apollo/persistence/progress-repo
  - apollo/schemas/problem
last_verified: 2026-07-27
stub: false
---

# handlers/done — the grade-of-record ORCHESTRATOR

`handle_done` is `POST /apollo/sessions/{id}/done`. It **assembles the whole
grade path** — the grading-path recipe (D21) starts here. The cross-cutting
grading invariants (grading-lane, misconceptions-empty, composite-retired,
score→letter→narrative) live in `overseer/_index`; this doc links there rather
than restating them.

## Interface

- `handle_done(*, db, neo, session_id) -> dict` — the only public entry (called
  by `routing/router`, and by `handlers/chat` when the intent/questioning gate
  decides "done"). Returns the student grade payload (`rubric`, `topics`,
  `progress`, `scorecard`, `grading_provenance`, `transcript`, …).

## Data flow

Ordered grade assembly (each step delegates to the owner doc):

1. Load session + problem (`_find_problem`) + latest `ProblemAttempt`; read the
   student graph (tolerating degraded Neo4j) then `store.freeze(session_id)`.
2. Derive the reference graph via `Problem.to_kg_graph` (`schemas/problem`).
3. **Transcript coverage** (the sole grader): `compute_transcript_coverage_with_spans`
   (`overseer/transcript-coverage`) over `_full_transcript` → coverage + validated
   evidence spans.
4. `compute_rubric` (`overseer/rubric`) maps coverage into the axis rubric.
5. **Topic score** (`_compute_topic_score_safe` wrapping `compute_topic_score` /
   `compute_centrality`, `overseer/topic-score`): best-effort, computed always.
   On success `served_rubric` REPLACES `overall` with the topic score/letter
   (new dict; `rubric` itself is never mutated).
6. `generate_diagnostic` (`overseer/diagnostic`) — grounded narrative plus, on
   topic-score JSON success, structured per-topic feedback from the student's
   verbatim utterances.
7. XP: `compute_xp_earned`/`compute_progress_envelope`/`apply_xp`
   (`overseer/xp` + `persistence/progress-repo`); reattempt detection via
   `has_prior_graded_attempt` (`persistence/done-write-linkage`).
8. Persist canonical artifact `write_artifacts` (`grading-artifact-writer`) →
   then project mastery `_project_mastery` → `update_mastery_from_artifact`
   (`projections/mastery`), and render `render_scorecard` (`projections/scorecard`).

## Invariants & gotchas

- **The transcript adjudicator is THE only grading lane.** A `CoverageGradingError`
  is NOT caught — it propagates to the retryable 503 handler; grading never falls
  back to an empty-graph or legacy-coverage grade. A degraded KG never yields a
  false F (grading reads the transcript, not the frozen graph).
- **`_compute_topic_score_safe` is soft-fail**: any exception → `topic_score=None`,
  `served_rubric is rubric` (byte-identical), and `topics` is absent (not null).
- **Structured feedback is additive and topic-only:** successful topic JSON is
  served as `student_response["feedback"]`; parse/LLM failure or no topic score
  leaves that key absent. `diagnostic_narrative` always remains the string
  back-compat surface (flattened structured output on success, legacy output on
  soft-fail).
- **Artifact write + artifact-derived mastery are own-failure-domain telemetry** —
  each owns its commit and swallows exceptions; neither can void the served grade.
  `_project_mastery` is skipped when `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is on (the
  dormant Bayesian path would double-apply evidence).
- The persisted `attempt.diagnostic_report` stores `{narrative, rubric (RAW),
  coverage, served_overall}`. `served_overall` (2026-07-26) is a snapshot of
  `served_rubric["overall"]` — the grade the student was actually shown (topic
  score when it computed). Re-serving surfaces (`handlers/browse` grade cards,
  `handlers/progress` recents) read the snapshot first and fall back to
  `rubric.overall` for pre-snapshot rows; `rubric` itself deliberately stays
  the RAW axis rubric for rerun/janitor consumers.
- The response keeps historical `graph_lane: null` for API compatibility.
- **Does NOT import `done_turn_order`** (the WU-4C1 shadow chain — A7 removed it).

## Env flags

- `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (`_graph_sim_layer3_enabled`) — gates the
  mastery-projection interlock; default OFF everywhere.

## Related

See `overseer/_index` for the grading-path cross-cutting invariants and the full
directional chain: `transcript-coverage ↔ rubric ↔ topic-score ↔ done ↔
grading-artifact-writer ↔ scorecard ↔ mastery`.
