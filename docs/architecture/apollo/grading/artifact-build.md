---
doc: apollo/grading/artifact-build
description: The pure builder for the canonical transcript/topic grading artifact payload, plus the package facade.
owns:
  - apollo/grading/artifact_build.py
  - apollo/grading/__init__.py
related:
  - apollo/overseer/topic-score
  - apollo/projections/scorecard
  - apollo/grading/event-model
  - apollo/conversation/handlers/grading-artifact-writer
last_verified: 2026-08-08
stub: false
---

# Grading artifact build — canonical payload builder

`build_llm_artifact` is the pure builder for the one canonical grading artifact
payload. `handlers/artifact_writer.py` persists its dict to
`internal.grading_runs`; [scorecard](../projections/scorecard.md) and campaign's
`SqlArtifactReader` consume/reconstruct it.

## Interface

- `build_llm_artifact(*, coverage, rubric, latency_ms, clarification_trace,
  topic_score=None) -> dict`.
- `GRADER_USED_LLM_FALLBACK` / `GRADER_USED_LLM_TRANSCRIPT` — grader-version
  constants (imported by `handlers/artifact_writer.py`).
- `LEDGER_STATUS_UNPROBED` (`"unprobed"`) — the third `node_ledger` status
  (2026-08-07 P1.2b).
- The facade `__init__.py` re-exports `build_llm_artifact` + those constants +
  the [event-model](event-model.md) symbols.

## Data flow

From `coverage.per_step` it derives `node_coverage` and a `node_ledger`
(`credited` / `unresolved` / `unprobed` rows, each carrying the adjudicator's
`basis`). `scores.composite` is set to the normalized
`rubric["overall"]["score"]/100`; `scores.llm_rubric` carries the whole rubric.
When a `TopicScoreResult` is passed, `scores.topic_score =
serialize_topic_score(topic_score)` (the single serializer). The dict also carries
`versions`, empty `edge_ledger` / `misconceptions`, `clarification_trace`, an
`abstention` block, and `grading_latency_ms`.

## Invariants & gotchas

- **Pure, no IO.** Deterministic reshape of its inputs.
- **`scores.composite` is the legacy axis blend, not the served grade.** It comes
  from `rubric["overall"]` (the axis rubric), which `done.py` does NOT serve —
  the served grade of record is [topic-score](../overseer/topic-score.md). No
  `apollo.grading.composite` module exists.
- **`grader_used` is stamped `GRADER_USED_LLM_FALLBACK` (`"llm_fallback"`)** in the
  built payload; `GRADER_USED_LLM_TRANSCRIPT` is exported for consumers but not
  applied here (`done.py` sets `llm_transcript` on the separate
  `grading_provenance`).
- **`unprobed` ledger rows (2026-08-07 P1.2b).** A graded node the questioning
  loop never raised is dropped from the topic denominator with status
  `unprobed`, but the adjudicator still returned a verdict for it, so it is
  still in `coverage.per_step`. Filing it as `unresolved` would render
  "Next time, explain X" in the scorecard's *missing or unclear* list while the
  same payload's `topics[]` says X was not part of this grade — so those keys
  (read off `topic_score.topics`) get their own status instead. It keeps a row
  (the record stays complete) and leaves BOTH sides of the `node_coverage`
  ratio. `topic_score=None` (soft-failed scoring) yields no `unprobed` rows at
  all — byte-identical to the pre-fix payload.
- **A new status is not automatically "safely ignored" downstream.** The
  student-facing [scorecard](../projections/scorecard.md) (`credited`/
  `unresolved`) and [mastery](../projections/mastery.md)
  (`credited`/`misconception`) do want it gone and drop it by construction. But
  [classroom](../projections/classroom.md)'s lowest-coverage query used to catch
  these exact nodes through its `unresolved` + NULL-span branch — "a graded node
  of this course is never taught or asked about" is the signal that branch
  exists for — so it matches `LEDGER_STATUS_UNPROBED` explicitly and reports
  `n_unprobed` alongside `mean_coverage` (review fix, 2026-08-08).
- **Every ledger row records `basis` — in its OWN key, not `method` (2026-08-08).**
  `basis ∈ {stated, used, implied, absent}` is the transcript adjudicator's own
  declaration of WHY a credit exists; until now it reached only a log line, which
  is why the 2026-08-08 replay could count the `absent`-yet-credited cell in
  aggregate but never per attempt. `method` stays the graph lane's resolver
  vocabulary (`exact` / `fuzzy` / `semantic` / `nli` / `clarification`, per
  migration 034's row comment) and remains `None` here — two enums in one field
  would make both unreadable, and the pair `(status, basis)` is the diagnostic.
  Read `coverage["basis"]` with `.get`: it is OPTIONAL both ways (the dormant
  graph lane emits none; every artifact written before 2026-08-08 has none), so a
  key with no basis lands as `None` rather than failing the write. Nothing scores
  on it — see [transcript-coverage](../overseer/transcript-coverage.md).
- `misconceptions` / `edge_ledger` are always empty; `abstention.abstained` is
  `None` (an LLM-only concept, lifted to `false` by the writer).
