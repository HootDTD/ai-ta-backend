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
last_verified: 2026-08-07
stub: false
---

# Overseer topic score — the served grade

Coverage-based topic scoring: this is the grade of record `done.py` serves (it
replaces `rubric["overall"]` with the topic score/letter). The retired
misconception detector contributes nothing, so every topic carries an empty
`misconceptions` tuple — the payload shape is retained for the UI.

## Interface

- `compute_topic_score(*, coverage, reference_nodes, centrality, evidence_spans=
  None, asked_node_ids=None) -> TopicScoreResult` — the live scorer.
  `asked_node_ids` (2026-08-07 P1.2b) is the `frozenset` of reference node ids
  with a `QuestionOpportunity` row this attempt; `None` = feature not wired
  (byte-identical to the pre-fix result).
- `compute_centrality(reference_graph) -> {node_id: weight}` — degree/position
  centrality over the reference `KGGraph` used to weight topics.
- `reference_statement_for(node) -> str | None` — renders ONE node's reference
  statement (ordered content fields joined with an em dash). Never the problem's
  worked solution.
- `REFERENCE_TEXT_CREDIT_THRESHOLD = 0.6` — the D2 reveal gate.
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
`CENTRALITY_W_MIN`, then normalized) over the PROBED subset (P1.2b, below). The
result's score reuses `rubric.score_to_letter`. `serialize_topic_score` shapes
the artifact's `scores.topic_score` block; `serialize_topics` shapes the served
`student_response["topics"]`. Both topic surfaces include the topic's gated
per-attempt `evidence_span` (string or null), its additive `hoot_assisted`
flag, and its additive `reference_text` (string or null). INTERACTION5:
`compute_topic_score` reads each node's flag from `coverage["hoot_assisted"]`
(absent → every topic `False`, byte-identical to the pre-feature result).

## Invariants & gotchas

- **This is the grade of record, not `composite`.** The served overall score and
  XP come from `TopicScoreResult`. The legacy axis `composite` is a separate,
  retained column (see [_index](_index.md) / [scorecard](scorecard.md)).
- **One serializer, two surfaces.** Both the persisted artifact and the served
  `topics` payload derive from `topic_score_serialize.py`, pinned to the design
  spec's field shape so they cannot drift. Each topic is
  `{canonical_key, display_name, credit, status, weight, evidence_span,
  hoot_assisted, reference_text, misconceptions}`; `hoot_assisted`
  (INTERACTION5) is additive and defaults `False`, `reference_text` (D2) is
  additive and defaults `None` — both absent-safe for old UI clients. Keep
  serialization separate from the pure `topic_score.py` computation module.
- **Empty misconceptions.** `TopicCredit.misconceptions` is always `()` and
  `TopicScoreResult.misconception_dock` is always `0.0`.
- **No graded nodes → 0.** An all-ungraded reference returns a zero result.
- **Only adjudicated nodes enter the denominator (2026-08-07 P0.5).** A graded
  node absent from BOTH `per_step` and `procedure_scores` was omitted by the
  adjudicator (abstain-not-zero, see `transcript-coverage`) — it is dropped and
  weights renormalize over the adjudicated set. No-op for historical/graph-lane
  coverage dicts (they carry every graded id). Graded nodes present but ZERO
  adjudicated raises `ValueError` (never a silent F(0)); `done.py`'s soft-fail
  wrapper converts that to the legacy rubric, and the serving lane already
  raises `CoverageGradingError` before reaching here in that state.
- **Never-probed graded nodes leave the denominator (2026-08-07 P1.2b).**
  `asked_node_ids` is the set of reference node ids with a `QuestionOpportunity`
  row THIS attempt — the questioning loop either asked about the node or
  recorded a tally update for it, so a topic the student taught spontaneously
  counts as probed. A graded, adjudicated node outside that set gets
  `weight = 0.0` and `status = "unprobed"` and still appears in `topics[]` (the
  artifact and the UI need to say "not part of this grade"). It can neither
  lower nor raise the score. Ordering note: the probed list is kept in reference
  order, never a set, so weight normalization sums the same floats every run —
  the grade must be reproducible. Degenerate case (the ledger names NO graded
  node, e.g. every ask went to `definition` nodes): fall back to grading every
  adjudicated node and log `apollo_topic_score_no_probed_graded_node` — the
  safety net must never make a Done ungradeable. `asked_node_ids=None` is
  byte-identical to the pre-fix result. P0.5's abstain filter runs FIRST, so an
  un-adjudicated node is dropped entirely rather than reported `unprobed`.
- **`reference_text` is credit-gated, not caller-gated (D2).** A topic with
  `credit < REFERENCE_TEXT_CREDIT_THRESHOLD` (0.6) carries
  `reference_statement_for(node)`; at or above it the field is `None`. The gate
  lives here (this module owns the credit) so the serializer stays a dumb shape
  mapper and both surfaces can never disagree. The reveal is ONE node's
  statement — the full worked solution is never exposed, and nothing is exposed
  pre-grade.
- Centrality is cycle-safe: `DEPENDS_ON` out-degree + `PRECEDES` topological
  position, combined and rescaled into `[CENTRALITY_W_MIN, 1]`.

## Related

`compute_topic_score` is soft-failed by `done.py` (`None` → topics absent).
`score_to_letter` is owned by [rubric](rubric.md).
