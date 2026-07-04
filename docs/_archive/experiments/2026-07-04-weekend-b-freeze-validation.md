# §9 Freeze Validation — Worker B subset (fluid chunk @ 0f5d4be)

- **Date:** 2026-07-04 (run 03:06–03:20 local)
- **Freeze SHA:** 0f5d4be5cf7644791e350205a874d45e789f1a79 (checkout verified, branch `staging`, clean tracked tree)
- **Backend:** campaign_launch shim on 127.0.0.1:8000, shadow mode, NLI prewarmed, healthz ok before/during/after
- **Stack:** local Supabase :57322, Neo4j :57687, real OpenAI
- **Outputs:** `campaign/out/freezeval/` (untracked; driver = byte-identical copy of committed f1c driver with `freezeval-` student prefix)
- **Analyzer:** scratchpad/analyze.py (graph row = `graph_payload_for()`, same as committed f1c sanity_checks.py)

## Run outcome

16 fluid_mechanics personas: **ok=12, err=4**. All 4 errors are the 4 `vague_then_clarifies`
personas, failing instantly (≈2 s) with 500 at `POST /apollo/sessions/from_hoot`.

**Root cause (backend-freeze.log):** OpenAI **`insufficient_quota` 429** first hit
03:20:13 mid-corpus (during attempt 64, `strong__volumetric_flow_rate_find_Q`) —
its shadow chain failed (`apollo_graph_shadow_failure ... shadow isolated; live
grade unaffected`, so pair=no on that one attempt) and every session-create after
that 500'd at the first LLM call. This is an **API-billing exhaustion, not a
freeze-code failure**. Quota re-verified still exhausted at report time
(direct minimal API call → 429 insufficient_quota twice).

Consequences:
- Gradeable graph rows: **11** (12 ok minus attempt 64's missing pair artifact).
- Vague personas: **0 fresh freeze-machine data** (all 4 quota-killed). Vague-persona
  clarification evidence below leans on b0smoke (same machine, same driver, pre-freeze-restart).
- **Step 2 (linear_motion attempt) and Step 3 (WU-AAS re-mint): BLOCKED** — both
  require live OpenAI (persona chat / scrape-mint pipeline). Not attempted, to avoid
  minting a guaranteed-failed authored set into the course.

## Comparison table

| Metric | Mine (freezeval, fluid, n=11 graph rows) | A's F2 (full corpus @ 0f5d4be, 34 gradeable) | b0smoke (fluid 16, same machine) | committed f1c (36 personas, 31 ok) |
|---|---|---|---|---|
| Gradeable abstain | 11/11 (100%) | 34/34 (100%) | 16/16 (100%) | 31/31 (100%) |
| Abstention reasons | `unresolved_rate_above_threshold` ×11, sole | sole `unresolved_rate_above_threshold` | sole, ×16 | ×31 + 1 `min_parser_confidence_below_threshold` |
| Expected-credit recall | **1.00 on all 11** | 0.88–1.0 | n/a (not computed then) | n/a |
| Clarification traces non-empty | 11/11 (misc 3/3, partial 4/4, strong 4/4; vague 0 data) | 33/34 | 16/16 incl. vague 4/4 | 31/31 |
| variable_mapping crashes | 0 (zero mentions in whole server log) | 0 | 0 | 0 |
| Grading p50 / p95 | 9.4 s / 11.8 s | 11.3 s / 15.6 s | 10.7 s / 20.7 s | 19.0 s / 29.1 s |
| Control-credit leaks | 8 rows, all `cond.incompressibility` | 7, same signature | 11 (`cond.incompressibility`) | 15 (9 incompr. + 6 macro cond.*) |
| Misconception detection | 0/3 personas, 0/11 rows (`misconceptions[]` empty) | 0/8 | 0/16 | 0/31 |
| Composite mean strong | 0.828 (n=5) | — | 0.758 (n=5) | 0.839 (n=10) |
| Composite mean partial | 0.348 (n=4) | — | 0.348 (n=4) | 0.564 (n=8) |
| Composite mean misconception | 0.670 (n=3) | — | 0.737 (n=3) | 0.734 (n=7) |

## Per-claim verdicts vs Worker A's F2

| A's claim | Verdict | Evidence on this machine |
|---|---|---|
| All gradeable abstain, sole reason `unresolved_rate_above_threshold` | **AGREE** | 11/11 abstained, reasons histogram exactly `{unresolved_rate_above_threshold: 11}` |
| Expected-credit recall 0.88–1.0 | **AGREE** | recall = 1.00 on every one of 11 graph rows (within/above A's band) |
| Clarification traces non-empty 33/34 | **AGREE (partial coverage)** | 11/11 non-empty here (misc/partial/strong). Vague personas got no fresh data (quota), but b0smoke on this machine had vague 4/4 non-empty with the identical driver |
| Zero variable_mapping crashes | **AGREE** | 0 occurrences in backend-freeze.log; 0 attempt-side crashes (all 4 errors were quota 500s) |
| Grading p50 11.3 s / p95 15.6 s | **AGREE** | 9.4 s / 11.8 s — same order, slightly faster (this machine, smaller subset); consistent with b0smoke 10.7/20.7 |
| Control-credit leaks 7, `cond.incompressibility` signature | **AGREE** | 8 leak rows, keys = `{cond.incompressibility: 8}` — identical signature, comparable rate (8/11 vs b0smoke 11/16) |
| Misconception detection 0/8 (G1-gated post-#94) | **AGREE** | 0/3 misconception personas asserted; `misconceptions[]` empty on all 11 rows; served `watch_out` = [] on all 3 |

## #94 seam probe (misconception personas' pair artifacts)

Even with 0 assertions clearing the floor, the seam demonstrably routes:

- `misconception__bernoulli_horizontal_pipe_find_p2`: **`misc.pressure_velocity_same_direction` appears as a
  clarification-resolution `candidate_key`** (state=vague, credit=denied) — a misc.* key
  live in the candidate set, exactly the #94 routing seam working below the assertion floor.
- The other two misconception personas: no misc.* candidate keys surfaced; their
  expected `misc.density_ignored` never appeared as a candidate. No `contradict*`
  fields anywhere in any pair payload.
- All 3 personas: `misconceptions[]` empty, `watch_out` [] — matching A's G1-gated 0/8.

## Composite noise check vs b0smoke

partial identical (0.348 vs 0.348); strong +0.070 (n=5); misconception −0.067 (n=3).
Within noise at these n. **AGREE** that the freeze code grades equivalently to b0smoke on this machine.

## Anomalies

1. **OpenAI quota exhaustion at 03:20:13** killed the last 4 (vague) personas and one
   pair artifact. Infrastructure/billing, not code. Blocks steps 2–3 until credits restored.
2. Attempt 64's shadow-chain 429 was **correctly isolated** — served LLM grade unaffected,
   error logged as `apollo_graph_shadow_failure ... shadow isolated`. Good freeze-code
   degradation behavior, worth noting as a positive.
3. No other tracebacks in the entire server run; all 5 are quota-caused.

## Outstanding (blocked on OpenAI credits)

- Re-run the 4 vague_then_clarifies personas (completes the clarification-trace claim on freeze code).
- Step 2: linear_motion strong attempt vs concept_id=5 (driver ready: `campaign/out/freezeval/run_linear_motion.py`).
- Step 3: WU-AAS re-mint + cross-upload dedup (#89) observation (driver ready: `campaign/out/freezeval/provision_wu_aas.py`).
