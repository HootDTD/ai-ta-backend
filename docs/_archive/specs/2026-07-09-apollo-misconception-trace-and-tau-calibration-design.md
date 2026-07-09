> **SUPERSEDED (Phase 2 only) — 2026-07-09:** the tau/co-key **calibration** direction below was DISPROVEN by the Phase-1 trace + a live instrumented run — judge confidence is ~1.0 on docks, misses, AND controls alike, so NO threshold separates them. Naming is the failure, not confidence. Phase 2 is replaced by the **structural co-key ("F-struct")** design: `docs/_archive/specs/2026-07-09-apollo-misconception-struct-cokey-design.md` (judge localizes / graph names via `opposes`). Phase 1 (the trace) is still live and is extended by F-struct.

# Apollo misconception detector — diagnostic trace (Phase 1) + tau/co-key calibration (Phase 2)

**Date:** 2026-07-09
**Branch:** `feat/apollo-misc-trace` (off `feat/apollo-judge-authoritative-gate`, local-only, HEAD ~`a982476`).
**Owner doc:** `docs/architecture/apollo.md` (`apollo/overseer/misconception_detector/**`).
**Predecessor:** recall-gap handoff `docs/_archive/handoffs/2026-07-08-apollo-misconception-recall-gap-handoff.md`; build handoff `…-detector-handoff.md`; corroboration/keying redesign spec `…-corroboration-redesign.md`.

## Problem

The detector works (recall 9/16, control FP 0/4) but we cannot see WHY 7 attempts miss.
The pedagogically-critical misses — attempts **88, 95, 100, 112** (all `nominal_for_real`,
still banded **Strong** = the 4 residual false-Strong) plus **102** (`density_ignored`) — dock
on the contrast case **105** (same concept, same bank). The gate reduces (handoff §3, §5-T5) to
"does the judge NAME a valid bank code AND CLEAR its routed tau", because `bank_pattern` already
emits the correct below-floor code and co-key is floor-free. So the fight is entirely in the
judge's verdict/confidence — but the gate collapses each per-concept group to ONE representative,
so a plain run reveals nothing per node. We need permanent, flag-guarded tracing.

## Phase 1 — the trace (THIS change; instrumentation only)

**Non-goal / hard constraint:** change NO tau, threshold, gate row, or scoring. Flag OFF ⇒
byte-identical behaviour and output.

- **Flag:** `APOLLO_MISC_TRACE` (default OFF), a SEPARATE flag from
  `APOLLO_MISCONCEPTION_DETECTOR` (so the trace can be switched on for a diagnostic run without
  changing whether the penalty applies). `config.trace_enabled()`, call-time read, same `_TRUTHY`
  set as `detector_enabled()`. Path: `APOLLO_MISC_TRACE_PATH`, default
  `campaign/out/misconception_trace.jsonl` (`config.trace_path()`).

- **Output format — JSONL** (chosen over structured logging): one machine-parseable JSON object
  per line, one line per reference-graph concept node. JSONL is trivially `json.loads`-per-line
  aggregatable by the calibration script Phase 2 needs, survives multi-attempt appends, and does
  not entangle the diagnostic with the app's log config/levels. The emit soft-fails (a trace
  defect must never break a grade).

- **Mechanism — a separate read-only replay module `trace.py`, NOT inline gate/merge hooks.**
  The gate collapses each anchor group to one representative, so an inline hook cannot cheaply
  attribute a decision back to every node without changing hot-path control flow. Instead
  `build_node_traces(...)` takes the SAME artifacts the live chain already produced — the raw
  `DetectionResult`, the `gate_findings` output, the `MergeOutcome`, the centrality map, the
  reference graph — and RE-DERIVES, per node, what the judge said and which §5 row fired. It never
  mutates inputs and never calls gate/merge internals, so it cannot perturb the grade it observes.
  The gate row is a *label* only (`row3_cokey_dock` / `row3b_cokey_clarify` / `row5_lone_solo_dock`
  / `row6…` / `row7…` / `row8…` / `row1_2_sympy` / `no_judge`); the actual dock/clarify/drop is
  read from the real gated output joined by `concept_key` (== `node_id`).

- **Per-node payload** (the recall-gap diagnostics of handoff §5): `attempt_id`, `node_id`,
  `node_type`; `judge:{verdict, misconception_code, confidence, verdict_token_prob_present}` (the
  T1/T3 logprob-vs-verbalized bit); `finding_signature`, `bank_code` (the co-key key);
  `bank_pattern_top1:{bank_code, similarity, above_floor}` (the below-floor best match, e.g.
  `nominal_for_real`@0.582); `cokey_bank_code`; `centrality`; `gate_decision`; `gate_row`;
  `ceiling_eligible`; per-attempt roll-up `final_band`, `misconception_penalty`, `ceiling_applied`,
  `is_false_strong`.

- **Attempt-agnostic:** controls (77, 89, 97, 106) are traced identically. `is_false_strong` is a
  misconception-class-only roll-up (a control is never false-Strong by definition), computed from
  the labeled `is_control` + the band. The live `done.py` seam has no persona label, so it passes
  `is_control=False` + the letter rubric band; the campaign harness supplies the real labeled
  `is_control` + the scorecard "Strong" band, so ITS `is_false_strong` is the true residual
  false-Strong the recall gap targets.

- **Wiring (both behind `trace_enabled()`):** `done.py`'s detector block (in its OWN nested
  try/except, so a trace defect never perturbs the already-computed grade — instrumentation must
  not change the grade even by soft-failing it) and `campaign/validate_misconception_detector.py`.

## Phase 2 — data-driven tau / co-key relaxation calibration (PLANNED, not in this change)

**Order: LOG FIRST, THEN DECIDE.** Run the Phase-1 trace over the labeled 20-set once (§ command
below), then read the per-node rows to localize which theory dominates (handoff §5): is the judge
naming the code but sub-tau (T1/T3 — check `confidence` vs its routed tau and
`verdict_token_prob_present`), or still hedging to `needs_clarification` (T2/T4)? Only then choose:

1. **If named-but-sub-tau (T1/T3):** calibrate `TAU_FIRE_VERBALIZED` / `TAU_SOLO_JUDGE` on the
   labeled set via ROC/PR (NOT hand-picked), OR relax the *co-key* path to not require routed-tau
   when two tiers already agree on the same validated `bank_code` (corroboration is itself
   evidence).
2. **If still hedging (T2/T4):** harden the judge prompt for decisiveness, OR switch to
   misconception-centric confirmation (bank proposes code → judge confirms yes/no). Keep keying /
   validation intact.

**HARD CONSTRAINT (non-negotiable):** control false-positives MUST stay **0/4**. Any tau loosening
or co-key relaxation is re-checked against all 4 controls (77, 89, 97, 106) after the change;
n=20 is thin, so treat the final tau values as a pre-env-flip calibration pass needing broader
validation. Success = residual false-Strong drops below 4 AND control FP stays 0.

## Reproduction (Phase-1 trace over the labeled 20-set, NLI OFF)

```bash
REPO=/c/Users/ultra/OneDrive/TA-test/ai-ta-backend
PYTHONPATH="$REPO" \
  APOLLO_MISCONCEPTION_DETECTOR=1 \
  APOLLO_MISC_TRACE=1 \
  APOLLO_MISC_TRACE_PATH="$REPO/campaign/out/misconception_trace.jsonl" \
  APOLLO_NLI_PREWARM=0 APOLLO_NLI_ENABLED=0 \
  python -m campaign.validate_misconception_detector > out.json 2> out.log
# Then aggregate: jq -s 'group_by(.attempt_id)' campaign/out/misconception_trace.jsonl
# (ids 75,77,81,88,89,95,97,100,102,105,106,108,109,110,111,112,113,114,115,116;
#  DSNs/OpenAI key auto-loaded from .env.campaign; re-seed the local Docker stack if down.)
```

## Test / coverage

Flag-OFF golden (trace never called, output byte-identical) + flag-ON payload-shape/gate-row tests
+ emit/soft-fail tests, in `apollo/overseer/tests/test_misconception_detector_trace.py` and the
`done.py` goldens in `apollo/handlers/tests/database/test_done_misconception.py`. Patch coverage
100% on the Phase-1 diff (`trace.py`/`config.py`/`done.py`); full apollo suite green, no regressions.
