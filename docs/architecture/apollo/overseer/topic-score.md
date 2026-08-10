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
last_verified: 2026-08-08
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
  the questioning loop actually ENGAGED with this attempt — the caller derives
  it (`handlers/done._probed_node_ids`), and a bare `QuestionOpportunity` row is
  deliberately not enough; `None` = feature not wired,
  which reproduces the pre-fix GRADE ARITHMETIC (score / letter / per-topic
  credit, weight, status) exactly. It is not a byte-identical payload: the
  additive `reference_text` is populated from the credit alone, independently
  of `asked_node_ids`, so a replay diff sees it on any attempt with a weak
  topic.
- `compute_centrality(reference_graph) -> {node_id: weight}` — degree/position
  centrality over the reference `KGGraph` used to weight topics.
- `reference_statement_for(node) -> str | None` — renders ONE node's reference
  statement (ordered content fields joined with an em dash). Never the problem's
  worked solution.
- `graded_topics_only(result) -> result` — the narrative/feedback VIEW: the same
  grade with `unprobed` topics removed (`None` passes through). Used by
  `done.py` for `generate_diagnostic` + `add_remediation_reviews` only.
- `REFERENCE_TEXT_CREDIT_THRESHOLD = 0.6` — the per-topic D2 reveal gate;
  `MAX_REFERENCE_TEXT_REVEALS = 2` — the per-attempt cap.
- `MIN_GRADED_DENOMINATOR = 2` — the absolute half of the P1.2b denominator
  floor; `UNPROBED_CREDIT_KEEP_THRESHOLD = 0.6` — the credit at which a
  ledger-less node is graded anyway. Plus the `TopicScoreResult`, `TopicCredit`,
  `TopicMisconception` value objects.
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
- **Empty misconceptions:** `TopicCredit.misconceptions` is always `()`,
  `misconception_dock` always `0.0`. **No graded nodes → a zero result.**
- **Only adjudicated nodes enter the denominator (2026-08-07 P0.5).** A graded
  node absent from BOTH `per_step` and `procedure_scores` was omitted by the
  adjudicator (abstain-not-zero, see `transcript-coverage`) — it is dropped and
  weights renormalize over the adjudicated set. No-op for historical/graph-lane
  coverage dicts (they carry every graded id). Graded nodes present but ZERO
  adjudicated raises `ValueError` (never a silent F(0)); `done.py`'s soft-fail
  wrapper converts that to the legacy rubric, and the serving lane already
  raises `CoverageGradingError` before reaching here in that state.
- **Never-engaged, uncredited graded nodes leave the denominator (2026-08-07
  P1.2b).** `asked_node_ids` is the set of reference node ids the questioning
  loop ENGAGED with this attempt (it really asked, the tally concluded something
  beyond bare `missing`, or the student quoted evidence for it — see
  `handlers/done`). A graded, adjudicated node outside that set gets
  `weight = 0.0` and `status = "unprobed"` and still appears in `topics[]` (the
  artifact and the UI need to say "not part of this grade"). `_denominator` is
  the single owner of the rule and returns reference order, never a set, so
  weight normalization sums the same floats every run — the grade must be
  reproducible. P0.5's abstain filter runs FIRST, so an un-adjudicated node is
  dropped entirely rather than reported `unprobed`.
- **The exclusion is ASYMMETRIC.** A ledger-less node whose credit is
  `>= UNPROBED_CREDIT_KEEP_THRESHOLD` (0.6 — the lowest P1.1 anchor meaning "the
  student landed this") is graded normally. The adjudicator reads the
  transcript, not the ledger, so it scores work the questioning loop never
  recorded a row for (~15% of ledger-less graded nodes were not `missing`);
  dropping those would confiscate the credit from the numerator AND hide them
  from the narrative/remediation, which see `graded_topics_only`.
- **…and it is bounded below by the denominator FLOOR (`_denominator_floor`):
  `>= MIN_GRADED_DENOMINATOR` AND `>= ceil(graded/2)`**, capped by the graded
  count so a 1-node rubric is never blocked. Below the floor the denominator is
  WIDENED back to it — dropped nodes returned highest-credit first, reference
  order breaking ties — and `apollo_topic_score_denominator_widened` is logged.
  Widening, not restoring: the earlier build restored the FULL denominator,
  which withheld all relief from the budget-starved sessions P1.2b exists for
  (2 of 5 probed and both correct scored 40/D; it now scores 67). The floor
  still closes the exploit it was added for — 1 of 5 probed cannot renormalize
  to weight 1.0 and score A+ — and subsumes the zero-probed degenerate case, so
  the safety net never makes a Done ungradeable.
- **P1.2b is ONE-DIRECTIONAL and nearly inert on today's bank.** It drops a node
  only when ledger-less AND under the keep threshold, and the floor re-admits
  highest-credit first, so it can only raise a score (all 192 rows of the
  2026-08-08 replay, zero exceptions). MEASURED: 2 of the 106 gradable Week-4
  attempts have an excluded node; 1 changes score (+7). SUPERSEDES the earlier
  "16 of 135, 8 single-probed" estimate — a static ledger count, never a replay
  result. Against the 7 P1.4 re-authored problems it moves 23 of 86 rows (mean
  +6.45, max +44), but that is a CEILING: that replay's ledger holds OLD node ids
  so every re-authored node is unasked by construction, and the audit could only
  bracket the re-authored median in [73, 86]. P1.2b's value is CONTINGENT on P1.4.
- **`reference_text` is credit-gated AND capped (D2).** Per topic: `credit <
  REFERENCE_TEXT_CREDIT_THRESHOLD` (0.6) and not `unprobed` (a topic excluded
  from the grade gets no reveal — leakage with no diagnostic value). Per
  attempt: at most `MAX_REFERENCE_TEXT_REVEALS` (2) statements, lowest credit
  first, then most central, then reference order; a node rendering no prose is
  skipped without consuming a slot. The cap is what makes "never the full worked
  solution" true on a wholly-failed attempt, where EVERY topic is below the
  threshold and the union of an uncapped reveal would be the whole graded
  reference solution (still convertible into a grade while `restart_problem`
  is reachable from REPORT and browse is best-grade-wins — D3, deferred to P3).
  The gate lives here (this module owns the credit) so the serializer stays a
  dumb shape mapper and both surfaces can never disagree. Nothing is exposed
  pre-grade. `MAX_REFERENCE_TEXT_REVEALS` is the per-attempt budget for the
  WHOLE payload, not just this field: [diagnostic](diagnostic.md)'s consistency
  gate imports it for the reference wording its own gap sentences quote, and
  orders its picks by the same key, so the narrative names the same nodes.
- **`unprobed` topics are filtered OUT of narration, never out of the payload.**
  They carry credit 0, so any consumer that enumerates topics as gaps would name
  one as missed while the same payload calls it "not part of this grade" (defect
  U2). `graded_topics_only` is the view `done.py` hands to `generate_diagnostic`
  and `add_remediation_reviews`; the served `topics[]` and
  `scores.topic_score.topics` stay complete. The artifact's `node_ledger` files
  them under their own status — see
  [artifact-build](../grading/artifact-build.md).
- Centrality is cycle-safe: `DEPENDS_ON` out-degree + `PRECEDES` topological
  position, combined and rescaled into `[CENTRALITY_W_MIN, 1]`.

## Related

`compute_topic_score` is soft-failed by `done.py` (`None` → topics absent).
`score_to_letter` is owned by [rubric](rubric.md).
