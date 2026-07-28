---
doc: apollo/overseer/topic-score
description: The LIVE grade of record — coverage-weighted topic scoring plus the single shared serializer.
owns:
  - apollo/overseer/topic_score.py
  - apollo/overseer/topic_score_serialize.py
related:
  - apollo/overseer/rubric
  - apollo/overseer/transcript-coverage
  - apollo/overseer/topic-narrative
  - apollo/grading/artifact-build
  - apollo/conversation/handlers/done
last_verified: 2026-07-28
stub: false
---

# Overseer topic score — the served grade

Coverage-based topic scoring: this is the grade of record `done.py` serves (it
replaces `rubric["overall"]` with the topic score/letter). The retired
misconception detector contributes nothing, so every topic carries an empty
`misconceptions` tuple — the payload shape is retained for the UI.

## Interface

- `compute_topic_score(*, coverage, reference_nodes, centrality, evidence_spans=
  None) -> TopicScoreResult` — the live scorer.
- `compute_centrality(reference_graph) -> {node_id: weight}` — degree/position
  centrality over the reference `KGGraph` used to weight topics.
- `TopicScoreResult`, `TopicCredit`, `TopicMisconception` value objects.
- `_GRADED_NODE_TYPES` / `_display_name_for` — consumed by
  [transcript-coverage](transcript-coverage.md).
- `serialize_topic_score` / `serialize_topics` (`topic_score_serialize.py`) —
  the ONE serializer, imported by `handlers/done.py`, `grading/artifact_build.py`,
  and `handlers/artifact_writer.py`.

## Data flow

`done.py` → `_compute_topic_score_safe` calls `compute_topic_score` with the
transcript `coverage`, the reference nodes, and `compute_centrality(reference_
graph)`. Only `_GRADED_NODE_TYPES` (equation / condition / simplification /
procedure_step) nodes score; each node's credit comes from `coverage`
(`per_step` + `procedure_scores`), weighted by centrality (floored at
`CENTRALITY_W_MIN`, then normalized). The result's score reuses
`rubric.score_to_letter`. `serialize_topic_score` shapes the artifact's
`scores.topic_score` block; `serialize_topics` shapes the served
`student_response["topics"]`. Both topic surfaces include the topic's gated
per-attempt `evidence_span` (string or null) and its additive `hoot_assisted`
flag. INTERACTION5: `compute_topic_score` reads each node's flag from
`coverage["hoot_assisted"]` (absent → every topic `False`, byte-identical to the
pre-feature result).

## Invariants & gotchas

- **This is the grade of record, not `composite`.** The served overall score and
  XP come from `TopicScoreResult`. The legacy axis `composite` is a separate,
  retained column (see [_index](_index.md) / [scorecard](scorecard.md)).
- **One serializer, two surfaces.** Both the persisted artifact and the served
  `topics` payload derive from `topic_score_serialize.py`, pinned to the design
  spec's field shape so they cannot drift. Each topic is
  `{canonical_key, display_name, credit, status, weight, evidence_span,
  hoot_assisted, misconceptions}`; `hoot_assisted` (INTERACTION5) is additive and
  defaults `False` (absent-safe for old UI clients). Keep serialization separate
  from the pure `topic_score.py` computation module.
- **Empty misconceptions.** `TopicCredit.misconceptions` is always `()` and
  `TopicScoreResult.misconception_dock` is always `0.0`.
- **No graded nodes → 0.** An all-ungraded reference returns a zero result.
- Centrality is cycle-safe: `DEPENDS_ON` out-degree + `PRECEDES` topological
  position, combined and rescaled into `[CENTRALITY_W_MIN, 1]`.

## Related

`compute_topic_score` is soft-failed by `done.py` (`None` → topics absent).
`score_to_letter` is owned by [rubric](rubric.md).
