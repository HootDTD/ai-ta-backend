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
last_verified: 2026-08-12
stub: false
---

# Overseer topic score — the served grade

Coverage-based topic scoring: the grade of record `done.py` serves (it replaces
`rubric["overall"]` with the topic score/letter). Every topic's `misconceptions`
tuple is empty in production — see the P3.2 dark build below.

## Interface

- `compute_topic_score(*, coverage, reference_nodes, centrality, evidence_spans=
  None, asked_node_ids=None, misconceptions=None, ceiling_active=False) ->
  TopicScoreResult` — the live scorer.
  `asked_node_ids` (2026-08-07 P1.2b) is the `frozenset` of node ids the
  questioning loop ENGAGED with, derived by the caller
  (`handlers/done._probed_node_ids`; a bare `QuestionOpportunity` row is
  deliberately not enough). `None` = not wired, reproducing the pre-fix GRADE
  ARITHMETIC exactly — but NOT a byte-identical payload, since `reference_text`
  is populated from credit alone and a replay diff sees it on any weak topic.
- `compute_centrality(reference_graph) -> {node_id: weight}` — degree/position
  centrality over the reference `KGGraph` used to weight topics.
- `reference_statement_for(node) -> str | None` — ONE node's reference statement
  (ordered content fields, em-dash joined). Never the worked solution.
- `graded_topics_only(result) -> result` — the narrative/feedback VIEW: the same
  grade with `unprobed` topics removed (`None` passes through). Used by
  `done.py` for `generate_diagnostic` + `add_remediation_reviews` only.
- `REFERENCE_TEXT_CREDIT_THRESHOLD = 0.6` (per-topic D2 reveal gate),
  `MAX_REFERENCE_TEXT_REVEALS = 2` (per-attempt cap),
  `MIN_GRADED_DENOMINATOR = 2` (absolute half of the P1.2b floor),
  `UNPROBED_CREDIT_KEEP_THRESHOLD = 0.6` (credit at which a ledger-less node is
  graded anyway); `TopicScoreResult` / `TopicCredit` / `TopicMisconception`.
- `misconceptions` (`Mapping[node_id, MisconceptionSpec] | None`) /
  `ceiling_active` (2026-08-12 P3.2, seam S7) — wrongness containers + the DARK
  `CEILING_UNCORRECTED = 84`. `MisconceptionSpec` is a **structural** `Protocol`
  (`node_id`, `quote`, `resolved`), satisfied by `wrongness.WrongnessFinding`
  without importing it (that import would close the cycle
  `unified → selection → topic_score → wrongness`).
- `_GRADED_NODE_TYPES` / `_display_name_for` — consumed by
  [transcript-coverage](transcript-coverage.md).
- `serialize_topic_score` / `serialize_topics` (`topic_score_serialize.py`) —
  the ONE serializer, imported by `handlers/done.py`,
  `grading/artifact_build.py` and `handlers/artifact_writer.py`.

## Data flow

`done.py` → `_compute_topic_score_safe` calls `compute_topic_score` with the
transcript `coverage`, the reference nodes and `compute_centrality(reference_
graph)`. Only `_GRADED_NODE_TYPES` (equation / condition / simplification /
procedure_step) score; credit comes from `coverage` (`per_step` +
`procedure_scores`), weighted by centrality (floored at `CENTRALITY_W_MIN`,
normalized) over the PROBED subset (P1.2b, below); the letter reuses
`rubric.score_to_letter`. `serialize_topic_score` shapes the artifact's
`scores.topic_score`, `serialize_topics` the served
`student_response["topics"]`; both carry `evidence_span`, `reference_text` and
`hoot_assisted` (INTERACTION5, from `coverage["hoot_assisted"]`; absent → every
topic `False`, byte-identical to pre-feature).

## Invariants & gotchas

- **The grade of record, not `composite`.** The served score and XP come from
  `TopicScoreResult`; the legacy axis `composite` is a separate retained column
  ([_index](_index.md) / [scorecard](scorecard.md)).
- **One serializer, two surfaces.** Artifact and served `topics` both come from
  `topic_score_serialize.py`, pinned to the spec shape so they cannot drift:
  `{canonical_key, display_name, credit, status, weight, evidence_span,
  hoot_assisted, reference_text, misconceptions}`. `hoot_assisted`
  (INTERACTION5, `False`) and `reference_text` (D2, `None`) are additive and
  absent-safe for old UI clients; serialization stays out of the pure module.
- **Misconception containers are EMPTY in production (P3.2 dark build,
  2026-08-12):** `done.py` passes `misconceptions=` only at
  `APOLLO_WRONGNESS_LEVEL >= 3`, `ceiling_active=(level >= 4)`, flag default 0.
  **No graded nodes → a zero result.**
- **The P3.2 ceiling, and why it is safe** (spec §2.3 L4). Falsy
  `misconceptions` returns through an EARLY RETURN predating the feature, so
  levels 0-2 are byte-identical BY CONSTRUCTION (pinned by
  `test_topic_score_ceiling.PRE_FEATURE_DIGESTS` — sha256 of the serialized
  payload over an 8-case corpus generated against the pre-feature module).
  Level 3 fills the containers with `dock_points=0.0`, moving nothing else.
  Level 4 applies `served = min(raw, CEILING_UNCORRECTED)` ONCE after
  `coverage_component`; `misconception_dock = raw - served` is display-only and
  `coverage_component` stays raw. Properties, each with a named test: **P-1** 84
  is a B+ (`score_to_letter` imported, never re-declared;
  `test_ceiling_letter_bands.py` fails if the bands move under it) so a `min()`
  cannot produce a D or an F; **P-2** one `min()`, so N findings cost what 1
  costs and the call is idempotent; **P-3** no double jeopardy — INTERACTION5's
  0.5 aside cap sits below the 0.6 credit S2' requires. Fail-safe = miss: an
  unmatched node id is ignored (`apollo_topic_score_misconception_unmatched`),
  an all-resolved set never caps (D7), `ceiling_active` with no finding is
  inert. `dock_points` splits the dock EQUALLY by largest remainder over INTEGER
  points, so the lines sum exactly.
- **The rubric absent-axis hazard (spec §2.5) — a NEGATIVE requirement.** Never
  feed `compute_rubric`'s `misconception_scores` (nor
  `done._attempt_misconception_scores`, nor `TutoringMessage` metadata): a
  non-empty map flips `AXIS_WEIGHTS["misconception_corrected"] = 0.05` to
  present, rescales every other axis by 0.95 and moves
  `score_details.llm_rubric.overall` (100/100/100 + one unresolved → 98).
  `topics[].misconceptions` is a SEPARATE surface, pinned by
  `test_topic_score_absent_axis.py`.
- **Only adjudicated nodes enter the denominator (2026-08-07 P0.5).** A graded
  node absent from BOTH `per_step` and `procedure_scores` was omitted by the
  adjudicator (abstain-not-zero, see `transcript-coverage`) — dropped, weights
  renormalize over the adjudicated set (no-op for historical/graph-lane dicts).
  ZERO adjudicated raises `ValueError`, never a silent F(0); `done.py`
  soft-fails to the legacy rubric, the serving lane already 503'd earlier.
- **Never-engaged, uncredited graded nodes leave the denominator (2026-08-07
  P1.2b).** A graded, adjudicated node outside `asked_node_ids` gets
  `weight = 0.0` / `status = "unprobed"` and still appears in `topics[]` ("not
  part of this grade"). `_denominator` owns the rule and returns reference
  order, never a set, so weights sum the same floats every run. P0.5's filter
  runs FIRST, so an un-adjudicated node is dropped, not reported `unprobed`.
- **The exclusion is ASYMMETRIC.** A ledger-less node credited
  `>= UNPROBED_CREDIT_KEEP_THRESHOLD` (0.6, the lowest P1.1 "student landed
  this" anchor) is graded normally: the adjudicator reads the transcript, not
  the ledger, and scores work the loop never recorded (~15% of ledger-less
  graded nodes were not `missing`). Dropping those would confiscate credit and
  hide the node from narrative/remediation.
- **…and bounded below by the FLOOR (`_denominator_floor`):
  `>= MIN_GRADED_DENOMINATOR` AND `>= ceil(graded/2)`**, capped by the graded
  count so a 1-node rubric is never blocked. Below it the denominator is WIDENED
  back to the floor (dropped nodes highest-credit first, reference order breaking
  ties), logging `apollo_topic_score_denominator_widened`. Widening, not
  restoring — restoring withheld all relief from the budget-starved sessions
  P1.2b exists for (2 of 5 probed, both correct: 40/D → 67) — while still
  closing the 1-of-5-probed A+ exploit and the zero-probed case. Sizing in the
  module comment: ONE-DIRECTIONAL (only raises a score, 192/192 replay rows),
  nearly inert today (2 of 106 attempts, 1 moved +7), CONTINGENT on P1.4.
- **`reference_text` is credit-gated AND capped (D2).** Per topic: `credit <
  REFERENCE_TEXT_CREDIT_THRESHOLD` (0.6) and not `unprobed` (a topic outside the
  grade gets no reveal). Per attempt: at most `MAX_REFERENCE_TEXT_REVEALS` (2)
  statements, lowest credit first, then most central, then reference order; a
  node rendering no prose is skipped without consuming a slot. The cap makes
  "never the full worked solution" true on a wholly-failed attempt, where every
  topic is sub-threshold and an uncapped reveal would be the whole graded
  solution — recitable while `restart_problem` is reachable from REPORT and
  browse is best-grade-wins (D3, deferred). The gate lives here, not in the
  serializer. `MAX_REFERENCE_TEXT_REVEALS` is the per-attempt budget for the
  WHOLE payload: [diagnostic](diagnostic.md) imports it and orders by that key.
- **`unprobed` topics are filtered OUT of narration, never out of the payload.**
  They carry credit 0, so a consumer enumerating topics as gaps would name one
  as missed while the same payload calls it "not part of this grade" (defect
  U2). `graded_topics_only` is the view `done.py` hands to `generate_diagnostic`
  and `add_remediation_reviews`; the served `topics[]` and
  `scores.topic_score.topics` stay complete, and the artifact's `node_ledger`
  files them by status ([artifact-build](../grading/artifact-build.md)).
- Centrality is cycle-safe: `DEPENDS_ON` out-degree + `PRECEDES` topological
  position, combined and rescaled into `[CENTRALITY_W_MIN, 1]`.

## Related

`compute_topic_score` is soft-failed by `done.py` (`None` → topics absent).
`score_to_letter` is owned by [rubric](rubric.md).
