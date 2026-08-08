---
doc: apollo/overseer/transcript-coverage
description: The LIVE grader of record — one-call transcript adjudication that emits the frozen coverage verdict.
owns:
  - apollo/overseer/transcript_coverage.py
  - apollo/overseer/coverage_contract.py
  - apollo/overseer/__init__.py
related:
  - apollo/overseer/topic-score
  - apollo/overseer/rubric
  - apollo/overseer/coverage
  - apollo/overseer/grounding
  - apollo/overseer/aside-penalty
  - apollo/ontology/graph
  - apollo/conversation/handlers/done
last_verified: 2026-08-07
stub: false
---

# Overseer transcript coverage — the grader of record

Transcript-first, single-LLM-call coverage adjudication. This is the sole live
grading lane (see [_index](_index.md)); it grades from the dialogue, never the
frozen KG, so a Neo4j-degraded Done still grades.

## Interface

- `compute_transcript_coverage_with_spans(transcript, reference_graph, problem,
  *, course_evidence=None, hoot_asides=(), tally_context=None) ->
  (CoverageVerdict, spans)` — the live entry called by `handlers/done.py`. One
  adjudication call yields both the verdict and the narrative spans map.
- `compute_transcript_coverage(...)` — verdict only (byte-identical verdict;
  spans are deliberately not a coverage key). Same `course_evidence` /
  `tally_context` kwargs.
- `narrative_evidence_spans(verdicts, transcript) -> {node_id: span}` — the
  per-attempt quote gate for the narrative.
- `validate_span`, `build_transcript_grader_schema(include_hoot_assisted=False,
  *, credit_enum=True)`, `build_system_prompt(problem, *, course_evidence=None,
  hoot_asides=(), tally_context=None)`, `build_user_message(..., *,
  course_evidence=None, hoot_asides=(), tally_context=None)`, `NodeVerdict`
  (with additive `hoot_assisted: bool = False`), `CREDIT_ANCHORS`,
  `TallyContextEntry`.
- **`CREDIT_ANCHORS = (0.0, 0.6, 0.85, 1.0)`** — the four-point credit scale
  (2026-08-07 bimodal-fix P1.1). Declared in the structured-output schema AND
  enforced in code; see the invariants below.
- **`tally_context` (P1.3) is the cross-slice argument shape**
  `list[{node_id: str, state: str, times_asked: int, student_quote: str|null}]`
  — `TallyContextEntry` is the TypedDict for it, but a plain `list[dict]` is the
  contract (`done.py` builds it from the `QuestionOpportunity` rows it already
  loaded). Every field except `node_id` is optional and defensively normalized
  here (`state` outside the tally's own four states → `"missing"`, non-int or
  negative `times_asked` → `0`, non-string/blank `student_quote` → `null`, rows
  that are not mappings or name a non-rubric node → dropped). This module NEVER
  reads the DB.
- From `coverage_contract.py`: `CoverageVerdict` / `NegotiationCounts` TypedDicts
  + `validate_coverage_verdict` (the frozen verdict schema both this module and
  the dormant `coverage.py` must satisfy). `CoverageVerdict` gains one OPTIONAL,
  additive key, `hoot_assisted: {node_id: bool}` (INTERACTION5), present iff asides
  were graded; `validate_coverage_verdict` accepts it and rejects any other extra
  key. `__init__.py` is empty glue.

## Data flow

`done.py` passes the full `(role, content)` transcript + the reference `KGGraph`
(+ optionally the live `tally_context`). Only `_GRADED_NODE_TYPES` reference
nodes become rubric items. One `MAIN_MODEL` structured call (temperature 0),
made via `bounded_client()` (`agent/llm-client`, 2026-08-04 — was a bare
`OpenAI()`), returns per-node verdicts (covered / credit / confidence /
evidence_span / basis). Each parsed `credit` is snapped onto `CREDIT_ANCHORS`
BEFORE any consumer sees it. `_to_coverage_verdict` reduces the verdicts to
`per_step` + `procedure_scores` + `confidences` + zeroed `negotiation_counts`,
validated before return. `procedure_scores` (anchored credit) flow unchanged
into the [topic score](topic-score.md); `per_step` feeds the [rubric](rubric.md)
axes.

The adjudication user message is `PROBLEM → RUBRIC ITEMS → [COURSE EVIDENCE] →
[HOOT LOOKUP ANSWERS] → [LIVE TUTOR TALLY] → DIALOGUE`; every optional block is
inserted before the dialogue so the transcript — the only thing that earns
credit — is always last. The system prompt appends the matching frames in the
same order.

## Invariants & gotchas

- **Credit is a FOUR-POINT SCALE, not a continuum (2026-08-07 P1.1).**
  `CREDIT_ANCHORS = (0.0, 0.6, 0.85, 1.0)` is declared in the structured-output
  schema (`credit: {type: number, enum: [...]}`) AND enforced in code by
  `_snap_credit`, which quantizes any off-anchor verdict to the nearest anchor
  and logs `transcript_coverage_credit_snapped`. Ties snap DOWN (distances are
  rounded to 9 dp first so an exact midpoint such as 0.925 is recognised as a
  tie despite binary float error) — the grader never manufactures credit the
  model did not judge. This REVERSES the earlier "continuous credit passes
  through untouched" rule: under `gpt-5.1` the free scale collapsed to the
  extremes (129 of 259 prod topic credits exactly 0, 114 ≥ 0.9, 8 mid), which
  with 1–3 graded nodes per problem made a B unreachable. The prompt carries
  2–3 calibration exemplars per anchor, drawn from the Week-4 transcripts.
  Because the snap happens at parse time, BOTH downstream credit consumers
  ([topic score](topic-score.md), axis [rubric](rubric.md)) only ever see
  anchors; the one deliberate non-anchor value in the chain is produced later by
  the [aside-penalty](aside-penalty.md) flat cap (0.5), which is a penalty, not
  an adjudication, and is not re-anchored. (This invariant would normally live
  in the domain `_index`, but that router is at its hard 60-line size cap.)
- **The credit enum can never be a new failure mode.** The two provider attempts
  are deliberately asymmetric: attempt 1 sends the enum, attempt 2 rebuilds the
  schema with `credit_enum=False` (byte-identical to the pre-P1.1 build) and logs
  `transcript_coverage_credit_enum_downgraded`. A provider that rejects a numeric
  enum degrades to today's behaviour, and `_snap_credit` still anchors the
  result. That kwarg is the fallback, not a caller-facing option.
- **The live tally is prior context, never a rail (2026-08-07 P1.3, defect
  U1).** `tally_context` adds ONE data block + ONE prompt rule: a node the tally
  marked `understood` WITH a student quote needs an explicit, dialogue-cited
  reason to score below 0.85. Every other state (`tentative` / `missing` /
  `conflicting` / no row) carries no presumption and never caps credit. There is
  no code-level floor, cap, or credit — the burden of proof lives entirely in
  the prompt, so the adjudicator stays the grader of record. `tally_context=None`
  (every caller until `done.py` wires it) reproduces both prompts BYTE FOR BYTE.
  Rows are filtered to the rubric ids ONCE in `_adjudicate_verdicts`, so the
  system prompt's rule and the user message's block can never disagree about
  whether there is a tally to reason about; the re-adjudication retry re-sends
  the same context.
- **`per_step["covered"]` needs `verdict.covered` AND `credit >= 0.5`** — matches
  the graph lane's scored threshold so a deliberate partial (e.g. 0.7) is not
  zeroed for binary axis consumers; the continuous `credit` is never promoted to
  1.0.
- **`validate_span` is diagnostic only.** The serving lane never zeroes or
  downgrades credit on a failed span — it logs `span_ok` and feeds the offline
  `campaign/transcript_replay.py` gate.
- **`narrative_evidence_spans` keeps a span only** when it is a verbatim quote of
  ONE student message AND the verdict earned positive credit; hallucinated,
  Apollo-sourced, or cross-message-stitched spans are dropped.
- **`_finite01` guards non-finite credits** — `json.loads` accepts `NaN`/`Infinity`
  literals; a non-finite value raises → `CoverageGradingError`.
- **Omitted verdict = abstain, not zero (2026-08-07 P0.5, defect I5).**
  `_adjudicate_all_graded` wraps the adjudication: a graded node missing from
  `verdicts[]` triggers ONE semantic re-adjudication (first call's verdicts win
  for nodes both returned; log key `transcript_coverage_missing_verdict`); a
  node still missing is OMITTED from all coverage maps by
  `_to_coverage_verdict` (the topic lane then drops it from the denominator —
  see `topic-score`), never scored 0.0. No verdict for ANY graded node raises
  `CoverageGradingError`; a retry that dies on provider errors degrades to
  exclusion (partial verdicts are kept). The RAW legacy rubric reads an
  omission as not-covered, acceptable because it is not served when the topic
  score computes.
- **No-fallback:** 2 provider attempts, then `CoverageGradingError(stage=
  "transcript_adjudication")` → the 503 retryable handler, never a fabricated
  grade. `negotiation_counts` are always zero (the transcript lane doesn't
  negotiate).
- **The Hoot-aside frame is purely additive (INTERACTION5, default OFF).**
  `hoot_asides=()` (the default; flag off, or no lookup aside used) reproduces the
  prompt, the JSON schema, AND the coverage dict BYTE-FOR-BYTE — no
  `hoot_assisted` key, no schema property. Non-empty asides add the `HOOT LOOKUP
  ANSWERS` prompt block + the strict-schema `hoot_assisted` boolean, and the
  verdict carries a `hoot_assisted: {node_id: bool}` map keyed exactly like
  `procedure_scores` (a graded node with no verdict is `False`). An aside is
  untrusted data and is NEVER quotable as student evidence — the span gate stays
  transcript-only, so `hoot_assisted` earns no credit here; the flat cap it feeds
  is applied downstream by `aside_penalty` before rubric/topic-score.

## Related

Grade-of-record and composite-retirement invariants live in [_index](_index.md).
`MAIN_MODEL` is pinned in `platform/config-model-pins`.
