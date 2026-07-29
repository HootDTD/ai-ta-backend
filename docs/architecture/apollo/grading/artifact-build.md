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
last_verified: 2026-07-25
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
- The facade `__init__.py` re-exports `build_llm_artifact` + those constants +
  the [event-model](event-model.md) symbols.

## Data flow

From `coverage.per_step` it derives `node_coverage` and a `node_ledger`
(`credited` / `unresolved` rows). `scores.composite` is set to the normalized
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
- `misconceptions` / `edge_ledger` are always empty; `abstention.abstained` is
  `None` (an LLM-only concept, lifted to `false` by the writer).
