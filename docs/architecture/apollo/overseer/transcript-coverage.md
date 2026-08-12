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
last_verified: 2026-08-12
stub: false
---

# Overseer transcript coverage — the grader of record

Transcript-first, single-LLM-call coverage adjudication. This is the sole live grading lane (see
[_index](_index.md)); it grades from the dialogue, never the frozen KG, so a Neo4j-degraded Done
still grades.

## Interface

- `compute_transcript_coverage_with_spans(transcript, reference_graph, problem, *,
  course_evidence=None, hoot_asides=(), tally_context=None, wrongness_candidates=None) ->
  (CoverageVerdict, spans)` — the live entry called by `handlers/done.py`; one call yields both
  the verdict and the narrative spans map. `compute_transcript_coverage(...)` is the verdict-only
  twin (same kwargs minus `hoot_asides`; spans are deliberately not a coverage key), so
  `campaign/transcript_replay.py` can exercise the same lanes. `narrative_evidence_spans(verdicts,
  transcript)` is the per-attempt quote gate. Plus `validate_span`, `TallyContextEntry`,
  `NodeVerdict` (additive `hoot_assisted: bool = False`, `contradicted: bool = False`).
- `build_transcript_grader_schema(include_hoot_assisted=False, *, credit_enum=True,
  include_contradicted=False)`, `build_system_prompt(problem, *, course_evidence=None,
  hoot_asides=(), tally_context=None, reference_items=None, wrongness_candidates=None)`,
  `build_user_message(problem, reference_items, transcript, *, course_evidence, hoot_asides,
  tally_context, wrongness_candidates)`. **A direct caller must hand BOTH builders the same
  `reference_items`** — that is what makes each optional rule agree with its data block (see
  invariants).
- `CREDIT_ANCHORS = (0.0, 0.6, 0.85, 1.0)` — the four-point credit scale (2026-08-07 P1.1),
  declared in the schema AND enforced in code. `credit_enum_supported()` /
  `reset_credit_enum_support()` read and re-arm the process-level enum latch (observability + test
  isolation).
- **`tally_context` (P1.3) is a cross-slice argument shape** `list[{node_id, state, times_asked,
  student_quote|null}]` (`done.py` builds it from `QuestionOpportunity` rows it already loaded).
  Every field except `node_id` is defensively normalized here (unknown `state` → `"missing"`,
  non-int/negative `times_asked` → `0`, blank quote → `null`, non-mapping/non-rubric rows
  dropped); this module NEVER reads the DB. Its NAME and default are load-bearing: `done.py`
  passes it inside the sole grading lane, which is NOT soft-failed, so renaming or dropping it
  turns every Done into a 503 — `handlers/tests/test_done_question_ledger.py::
  test_coverage_entrypoint_really_accepts_tally_context` pins the unmocked signature so a bad
  merge reds CI instead of prod.
- **`wrongness_candidates` (P3.2) is the second-reader argument shape** `{node_id: student_quote}`
  — graded nodes only, each quote the per-turn tally's verbatim-verified student words (`done.py`
  derives it via `overseer/wrongness.candidate_quotes`), normalized the same defensive way.
  Supplied, it adds the strict-schema `contradicted` boolean, the `FLAGGED CLAIMS` block and the
  corroboration rule; answers ride back under `wrongness`.
- From `coverage_contract.py`: `CoverageVerdict` / `NegotiationCounts` TypedDicts +
  `validate_coverage_verdict` + `BASIS_VALUES` + `WRONGNESS_FLAGS`. Exactly THREE optional
  additive keys are allowed — `hoot_assisted: {node_id: bool}` (INTERACTION5), `basis: {node_id:
  stated|used|implied|absent}` (2026-08-08), `wrongness: {node_id: {contradicted, corrected_later,
  prompted}}` (P3.2); any other extra key is rejected.

## Data flow

`done.py` passes the `(role, content)` transcript + the reference `KGGraph` (+ optionally
`tally_context` / `wrongness_candidates`). Only `_GRADED_NODE_TYPES` reference nodes become rubric
items. One `MAIN_MODEL` structured call (temperature 0) via `bounded_client()`
(`agent/llm-client`) returns per-node verdicts (covered / credit / confidence / evidence_span /
basis); each `credit` is snapped onto `CREDIT_ANCHORS` BEFORE any consumer sees it.
`_to_coverage_verdict` reduces them to `per_step` + `procedure_scores` + `confidences` + `basis` +
zeroed `negotiation_counts`, validated before return; `procedure_scores` flows into the [topic
score](topic-score.md), `per_step` into the [rubric](rubric.md) axes, `basis` into the
`node_ledger`.

User message: `PROBLEM → RUBRIC ITEMS → [COURSE EVIDENCE] → [HOOT LOOKUP ANSWERS] → [LIVE TUTOR
TALLY] → [FLAGGED CLAIMS] → DIALOGUE`. Every optional block sits before the dialogue so the
transcript — the only thing that earns credit — is always last; the system prompt appends the
frames in the same order.

## Invariants & gotchas

- **Credit is a FOUR-POINT SCALE, not a continuum (2026-08-07 P1.1).** `CREDIT_ANCHORS` is
  declared in the schema AND enforced by `_snap_credit`, which quantizes off-anchor verdicts to
  the nearest anchor (logging `transcript_coverage_credit_snapped`) at PARSE time, so no
  downstream consumer ever sees one. Ties snap DOWN (distances rounded to 9 dp so a midpoint like 0.925
  is a tie despite float error) — never manufacturing credit the model did not judge. This REVERSES the earlier continuous-credit rule (the free scale
  collapsed to the extremes under `gpt-5.1`). That cross-consumer statement, and the one
  deliberate non-anchor value ([aside-penalty](aside-penalty.md)'s 0.5 cap), live in
  [_index](_index.md).
- **Exemplars ARE the calibration (≥2 per anchor), and each is a PARAPHRASE** of a real Week-4
  transcript — a copied clause would ride a pilot student's words into every future call
  (`test_transcript_coverage_exemplars.py` pins it).
- **No code clamp on the model's credit, and no prompt floor** — both measured and REJECTED
  2026-08-08; `covered` means FULLY covered, so `covered=False` + a mid credit is the normal shape
  of a genuine partial. The 0.6 anchor is still too cheap:
  `_archive/experiments/2026-08-08-apollo-06-floor.md`.
- **`basis` is PERSISTED and gates NOTHING (2026-08-08)**, so the next lever can be sized before
  it is written: the evidence class per node, keyed like `procedure_scores`, riding into every
  `node_ledger` row. An off-enum value is DROPPED (`transcript_coverage_basis_off_enum`), never
  raised on — a diagnostic must never 503 a grading the student earned.
- **The credit enum is neither a new failure mode nor a permanent tax.** ONLY an error matching
  `_is_schema_rejection` (a request-validation signature AND a schema subject — never a
  429/timeout/5xx, which would forge the "unsupported" signal) drops it; that path rebuilds with `credit_enum=False` (byte-identical to pre-P1.1), logs
  `transcript_coverage_credit_enum_downgraded`, latches the enum off for the PROCESS (a restart
  re-arms it, when a provider fix lands), and earns an EXTRA attempt so it never spends the retry
  a transient fault is entitled to. `_snap_credit` anchors either way.
- **All four optional frames are purely additive, and each RULE appears only with its DATA
  BLOCK.** `course_evidence=None` / `hoot_asides=()` / `tally_context=None` /
  `wrongness_candidates=None` each reproduce the prompts, the JSON schema AND the coverage dict
  BYTE-FOR-BYTE (P3.2's is sha256-pinned in `test_transcript_coverage_wrongness.py`).
  `_adjudicate_verdicts` normalizes rows ONCE and hands both builders the same ones, so
  `build_system_prompt` appends a rule only given `reference_items` AND ≥1 surviving row (else the
  model gets a rule about a block it cannot see); a context without `reference_items` is ignored and logged
  (`…_tally_rule_skipped` / `…_wrongness_rule_skipped`). For wrongness that one normalized result
  ALSO drives the schema field and the verdict key, and renders in rubric order so the prompt is
  deterministic. The re-adjudication retry re-sends it unchanged.
- **The live tally is prior context, never a rail (P1.3, defect U1).** It adds ONE data block +
  ONE rule: a node the tally marked `understood` WITH a student quote needs an explicit,
  dialogue-cited reason to score below 0.85. Every other state carries no presumption and never
  caps credit. No code-level floor, cap or credit — the burden of proof lives entirely in the prompt.
- **The Hoot-aside frame (INTERACTION5, default OFF)** adds the `HOOT LOOKUP ANSWERS` block + the
  strict-schema `hoot_assisted` boolean, and the verdict carries `hoot_assisted: {node_id: bool}`
  keyed like `procedure_scores`. An aside is untrusted data, NEVER quotable as student evidence;
  the flat cap it feeds is applied by [aside-penalty](aside-penalty.md).
- **The adjudicator is a CORROBORATOR, never a second detector (P3.2).** Given
  `wrongness_candidates` it may confirm or deny a finding the per-turn tally raised; it may
  **never originate** one and **never supplies the span** — a second independent detector
  re-creates defect U1, and its own spans fail `validate_span` far more often than the tally's
  (63.3% vs 0/465). Enforced structurally, not by prompt: `_to_coverage_verdict` emits a `wrongness` row
  ONLY for a flagged node, so a volunteered `contradicted: true` on an unlisted node is dropped.
  Rows are keyed like `procedure_scores`, so an omitted/abstained node has none — the second
  reader's SILENCE reads downstream as "not corroborated", never as a penalty (P0.5 applied to
  this lane). `contradicted` parses like `hoot_assisted` (absent ⇒ `False`, truthy non-bool
  raises); `corrected_later` and `prompted` were always emitted and always dropped — all three are
  finally read via this key.
- **`coverage["wrongness"]` is an `_OPTIONAL_KEYS` TRIPWIRE.** The contract is closed and
  `_to_coverage_verdict` validates on the way out of the sole grading lane, so emitting the key
  without listing it in `coverage_contract._OPTIONAL_KEYS` fails EVERY Done, including attempts
  with no finding; emitter and allowlist ship in one commit (`test_coverage_contract_wrongness.py`). Carrying it is inert for every existing consumer
  (`test_coverage_wrongness_downstream.py`) and cannot reach `compute_rubric`'s separate
  `misconception_scores` axis. Unchanged on purpose: `per_step` stays `{covered, missing}` and no
  `TopicStatus` is added — wrongness is a sibling record on the topic, never a status value.
- **`per_step["covered"]` needs `verdict.covered` AND `credit >= 0.5`** — matches the graph lane's
  scored threshold; the credit is never promoted to 1.0.
- **`validate_span` is diagnostic only** — the serving lane never zeroes or downgrades credit on a
  failed span; it logs `span_ok` and feeds the offline replay gate. **`narrative_evidence_spans`
  keeps a span only** when it verbatim-quotes ONE student message AND the verdict earned positive
  credit. **`_finite01`** rejects the `NaN`/`Infinity` literals `json.loads` accepts →
  `CoverageGradingError`.
- **Omitted verdict = abstain, not zero (P0.5, defect I5).** `_adjudicate_all_graded` triggers ONE
  semantic re-adjudication for graded nodes missing from `verdicts[]` (first call's verdicts win;
  log key `transcript_coverage_missing_verdict`); a node still missing is OMITTED from every
  coverage map, never scored 0.0, so the topic lane drops it from the denominator. No verdict for
  ANY graded node raises `CoverageGradingError`; a retry dying on provider errors degrades to
  exclusion. The RAW legacy rubric reads an omission as not-covered — it is not served when topic
  score computes.
- **No-fallback:** `_ADJUDICATION_ATTEMPTS` (2) like-for-like attempts, then
  `CoverageGradingError(stage="transcript_adjudication")` → the 503 retryable handler, never a
  fabricated grade. `negotiation_counts` are always zero.

## Related

Grade-of-record and composite-retirement invariants live in [_index](_index.md). `MAIN_MODEL` is
pinned in `platform/config-model-pins`.
