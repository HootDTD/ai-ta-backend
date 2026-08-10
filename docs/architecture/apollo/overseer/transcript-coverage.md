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
  *, course_evidence=None, hoot_asides=()) -> (CoverageVerdict, spans)` — the live
  entry called by `handlers/done.py`. One adjudication call yields both the verdict
  and the narrative spans map.
- `compute_transcript_coverage(...)` — verdict only (byte-identical verdict;
  spans are deliberately not a coverage key). Same `course_evidence` /
  `hoot_asides` kwargs.
- `narrative_evidence_spans(verdicts, transcript) -> {node_id: span}` — the
  per-attempt quote gate for the narrative.
- `validate_span`, `build_transcript_grader_schema(include_hoot_assisted=False)`,
  `build_system_prompt(problem, *, course_evidence=None, hoot_asides=())`,
  `build_user_message(..., *, course_evidence=None, hoot_asides=())`, `NodeVerdict`
  (with additive `hoot_assisted: bool = False`).
- From `coverage_contract.py`: `CoverageVerdict` / `NegotiationCounts` TypedDicts
  + `validate_coverage_verdict` (the frozen verdict schema both this module and
  the dormant `coverage.py` must satisfy). `CoverageVerdict` gains one OPTIONAL,
  additive key, `hoot_assisted: {node_id: bool}` (INTERACTION5), present iff asides
  were graded; `validate_coverage_verdict` accepts it and rejects any other extra
  key. `__init__.py` is empty glue.

## Data flow

`done.py` passes the full `(role, content)` transcript + the reference `KGGraph`.
Only `_GRADED_NODE_TYPES` reference nodes become rubric items. One `MAIN_MODEL`
structured call (temperature 0), made via `bounded_client()` (`agent/llm-client`,
2026-08-04 — was a bare `OpenAI()`), returns per-node verdicts (covered / credit /
confidence / evidence_span / basis). `_to_coverage_verdict` reduces them to
`per_step` + `procedure_scores` + `confidences` + zeroed `negotiation_counts`,
validated before return. `procedure_scores` (continuous credit) flow unchanged
into the [topic score](topic-score.md); `per_step` feeds the [rubric](rubric.md)
axes.

## Invariants & gotchas

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
