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
last_verified: 2026-08-08
stub: false
---

# Overseer transcript coverage — the grader of record

Transcript-first, single-LLM-call coverage adjudication. This is the sole live
grading lane (see [_index](_index.md)); it grades from the dialogue, never the
frozen KG, so a Neo4j-degraded Done still grades.

## Interface

- `compute_transcript_coverage_with_spans(transcript, reference_graph, problem,
  *, course_evidence=None, hoot_asides=(), tally_context=None) ->
  (CoverageVerdict, spans)` — the live entry called by `handlers/done.py`; one
  adjudication call yields both the verdict and the narrative spans map.
  `compute_transcript_coverage(...)` is the verdict-only twin (same kwargs minus
  `hoot_asides`; spans are deliberately not a coverage key).
  `narrative_evidence_spans(verdicts, transcript) -> {node_id: span}` is the
  per-attempt quote gate. Plus `validate_span`, `NodeVerdict` (additive
  `hoot_assisted: bool = False`), `TallyContextEntry`.
- `build_transcript_grader_schema(include_hoot_assisted=False, *,
  credit_enum=True)`, `build_system_prompt(problem, *, course_evidence=None,
  hoot_asides=(), tally_context=None, reference_items=None)`,
  `build_user_message(problem, reference_items, transcript, *, course_evidence,
  hoot_asides, tally_context)`. **A direct caller must hand BOTH builders the
  same `reference_items`** — that is what makes the tally rule and the tally
  block agree (see invariants).
- `CREDIT_ANCHORS = (0.0, 0.6, 0.85, 1.0)` — the four-point credit scale
  (2026-08-07 bimodal-fix P1.1), declared in the schema AND enforced in code.
  `credit_enum_supported()` / `reset_credit_enum_support()` read and re-arm the
  process-level enum latch (observability + test isolation).
- **`tally_context` (P1.3) is the cross-slice argument shape**
  `list[{node_id: str, state: str, times_asked: int, student_quote: str|null}]`
  (`done.py` builds it from the `QuestionOpportunity` rows it already loaded).
  Every field except `node_id` is defensively normalized here (unknown `state` →
  `"missing"`, non-int/negative `times_asked` → `0`, blank `student_quote` →
  `null`, non-mapping or non-rubric rows dropped). This module NEVER reads the DB.
  The parameter NAME and default are load-bearing: `done.py` passes
  `tally_context=` inside the sole grading lane, which is NOT soft-failed, so
  renaming or dropping it turns every Done into a 503.
  `handlers/tests/test_done_question_ledger.py::
  test_coverage_entrypoint_really_accepts_tally_context` pins the unmocked
  signature so a bad merge reds CI instead of prod.
- From `coverage_contract.py`: `CoverageVerdict` / `NegotiationCounts` TypedDicts
  + `validate_coverage_verdict` (the frozen verdict schema this module and the
  dormant `coverage.py` must satisfy). `CoverageVerdict` gains ONE optional
  additive key, `hoot_assisted: {node_id: bool}` (INTERACTION5); others rejected.

## Data flow

`done.py` passes the full `(role, content)` transcript + the reference `KGGraph`
(+ optionally the live `tally_context`). Only `_GRADED_NODE_TYPES` reference
nodes become rubric items. One `MAIN_MODEL` structured call (temperature 0) via
`bounded_client()` (`agent/llm-client`) returns per-node verdicts (covered /
credit / confidence / evidence_span / basis); each `credit` is snapped onto
`CREDIT_ANCHORS` BEFORE any consumer sees it. `_to_coverage_verdict` reduces the
verdicts to `per_step` + `procedure_scores` + `confidences` + zeroed
`negotiation_counts`, validated before return; `procedure_scores` (anchored
credit) flows into the [topic score](topic-score.md) and `per_step` into the
[rubric](rubric.md) axes.

The user message is `PROBLEM → RUBRIC ITEMS → [COURSE EVIDENCE] → [HOOT LOOKUP
ANSWERS] → [LIVE TUTOR TALLY] → DIALOGUE`; every optional block sits before the
dialogue so the transcript — the only thing that earns credit — is always last.
The system prompt appends the matching frames in the same order.

## Invariants & gotchas

- **Credit is a FOUR-POINT SCALE, not a continuum (2026-08-07 P1.1).**
  `CREDIT_ANCHORS` is declared in the structured-output schema (`credit: {type:
  number, enum: [...]}`) AND enforced by `_snap_credit`, which quantizes any
  off-anchor verdict to the nearest anchor and logs
  `transcript_coverage_credit_snapped`. Ties snap DOWN (distances rounded to 9 dp
  so a midpoint like 0.925 is a tie despite float error) — the grader never
  manufactures credit the model did not judge. This REVERSES the earlier
  "continuous credit passes through untouched" rule: under `gpt-5.1` the free
  scale collapsed to the extremes (129 of 259 prod topic credits exactly 0, 114 ≥
  0.9, 8 mid), which with 1–3 graded nodes made a B unreachable. The snap
  happens at parse time, so no downstream consumer ever sees an off-anchor credit;
  that cross-consumer statement (and the one deliberate non-anchor value,
  [aside-penalty](aside-penalty.md)'s 0.5 cap) lives in [_index](_index.md).
- **Exemplars ARE the calibration (≥2 per anchor), and each is a PARAPHRASE** of
  a real Week-4 transcript: a copied clause would ride a pilot student's words
  into every future call. `test_transcript_coverage_exemplars.py` pins that.
- **Two levers against the too-cheap 0.6 were measured and REJECTED 2026-08-08;
  the cheap 0.6 itself is still open.** A prompt content floor (`f625bcf`,
  REVERTED) moved nothing on its 8–27-char target and drove 5 of 6 deterministic
  node moves DOWN on long transcripts (one certified partial C(63)→F(8)). A code
  clamp on `covered=False` re-bimodalizes the grade (B 16→3, median 72→49):
  `covered` means FULLY covered, the normal shape of a genuine partial. Evidence
  + the prompt self-contradictions a retry must fix first:
  `_archive/experiments/2026-08-08-apollo-06-floor.md`.
- **The credit enum is neither a new failure mode nor a permanent tax.** ONLY an
  error matching `_is_schema_rejection` (a request-validation signature AND a
  schema subject — never a 429/timeout/5xx) drops it; that path rebuilds the
  schema with `credit_enum=False` (byte-identical to pre-P1.1), logs
  `transcript_coverage_credit_enum_downgraded`, latches the enum off for the
  PROCESS (a restart re-arms it, which is when a provider fix would land), and
  earns an EXTRA attempt so it never spends the like-for-like retry a transient
  fault is entitled to. Downgrading on a transient error would both grade that
  attempt under the unconstrained schema and forge the "enum unsupported" signal
  the calibration arm reads — hence the two-part match. `_snap_credit` anchors
  either way; `credit_enum=` is the fallback, not a caller-facing option.
- **The live tally is prior context, never a rail (2026-08-07 P1.3, defect
  U1).** `tally_context` adds ONE data block + ONE prompt rule: a node the tally
  marked `understood` WITH a student quote needs an explicit, dialogue-cited
  reason to score below 0.85. Every other state (`tentative` / `missing` /
  `conflicting` / no row) carries no presumption and never caps credit. No
  code-level floor, cap, or credit — the burden of proof lives entirely in the
  prompt, so the adjudicator stays the grader of record. `tally_context=None`
  (every caller until `done.py` wires it) reproduces both prompts BYTE FOR BYTE.
- **The tally RULE and the tally BLOCK appear or disappear together.** Both
  builders filter rows to the rubric ids, so `build_system_prompt` appends the
  rule only when given `reference_items` AND ≥1 row survives; a `tally_context`
  without `reference_items` is ignored and logged
  (`transcript_coverage_tally_rule_skipped`). Otherwise a tally naming only
  ungraded definition nodes — which `question_opportunities` rows naturally
  contain — would leave the model a rule about a block it cannot see.
  `_adjudicate_verdicts` filters once and hands both builders the same rows and
  items; the re-adjudication retry re-sends the same context.
- **`per_step["covered"]` needs `verdict.covered` AND `credit >= 0.5`** — matches
  the graph lane's scored threshold; the credit is never promoted to 1.0.
- **`validate_span` is diagnostic only** — the serving lane never zeroes or
  downgrades credit on a failed span; it logs `span_ok` and feeds the offline
  `campaign/transcript_replay.py` gate. **`narrative_evidence_spans` keeps a span
  only** when it verbatim-quotes ONE student message AND the verdict earned
  positive credit. **`_finite01`** rejects the `NaN`/`Infinity` literals
  `json.loads` accepts → `CoverageGradingError`.
- **Omitted verdict = abstain, not zero (2026-08-07 P0.5, defect I5).**
  `_adjudicate_all_graded` triggers ONE semantic re-adjudication for graded nodes
  missing from `verdicts[]` (the first call's verdicts win; log key
  `transcript_coverage_missing_verdict`); a node still missing is OMITTED from
  every coverage map, never scored 0.0, so the topic lane drops it from the
  denominator. No verdict for ANY graded node raises `CoverageGradingError`; a
  retry that dies on provider errors degrades to exclusion. The RAW legacy rubric
  reads an omission as not-covered — it is not served when topic score computes.
- **No-fallback:** `_ADJUDICATION_ATTEMPTS` (2) like-for-like provider attempts,
  then `CoverageGradingError(stage="transcript_adjudication")` → the 503
  retryable handler, never a fabricated grade. `negotiation_counts` are always
  zero (the transcript lane doesn't negotiate).
- **The Hoot-aside frame is purely additive (INTERACTION5, default OFF).**
  `hoot_asides=()` reproduces the prompt, the JSON schema, AND the coverage dict
  BYTE-FOR-BYTE. Non-empty asides add the `HOOT LOOKUP ANSWERS` block + the
  strict-schema `hoot_assisted` boolean, and the verdict carries
  `hoot_assisted: {node_id: bool}` keyed exactly like `procedure_scores`. An
  aside is untrusted data and NEVER quotable as student evidence; the flat cap it
  feeds is applied downstream by [aside-penalty](aside-penalty.md).

## Related

Grade-of-record and composite-retirement invariants live in [_index](_index.md).
`MAIN_MODEL` is pinned in `platform/config-model-pins`.
