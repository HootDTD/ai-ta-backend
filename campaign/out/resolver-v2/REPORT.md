# Resolver V2 — calibration + F1c replay verdict (2026-07-07, T8)

- **Branch:** `feat/apollo-resolver-v2` (base 271ff7a); design:
  `docs/_archive/specs/2026-07-07-resolver-v2-design.md` §9 / task card T8.
- **Corpus:** frozen F1c (`campaign/out/f1c/attempts.jsonl`, READ-only) — 31
  gradeable attempts replayed; 36 records (incl. 5 `status=error`) used by the
  DB-free calibration only. `git status campaign/out/f1c` → clean; the only
  dirty tree files are the 4 pre-existing `apollo/subjects/**` problem_02–05
  diffs, untouched and unstaged per the card.
- **Environment:** local Docker stack (Supabase :57322, Neo4j :57687),
  `.env.campaign`, `APOLLO_NLI_ENABLED=1 APOLLO_NLI_MISC_POSITIVE_CERTIFY=1
  APOLLO_ABSTENTION_COMPOSITE=1 HF_HUB_OFFLINE=1
  APOLLO_NLI_GRADING_MAX_NODES=40`; composite weights `w_n=0.706 w_e=0.294
  p=0.15` (renormalized 2026-07-07 — this OFF run re-baselines; older replay
  JSONs used 0.6/0.25 and are NOT comparable). Bands Strong≥0.85 /
  Proficient≥0.70 / Developing≥0.50 / Beginning.
- **ON run:** `APOLLO_RESOLVER_V2=1`, `APOLLO_RESOLVER_V2_GRAYZONE=0` — the
  design's pinned deterministic-replay default (§5 move 5, §8); zero LLM
  calls added.
- **Hygiene:** `degrading_without_nli` hits: 0 (both runs);
  `resolver_v2_failed_falling_back_to_v1` hits: 0; replay errors: 0/31.
- **Artifacts:** `calibration-2026-07-07.json` (sweep + negative_audit +
  held-out + engine check), `nli-pair-scores-2026-07-07.json` (raw NLI pair
  cache), `replay-off/on-2026-07-07.json`, `db-snapshot-off/on-2026-07-07.json`
  (per-attempt edge coverage — replay rows supersede in place, so snapshots
  were taken between runs), `traces-on-2026-07-07/` (31 full per-node traces),
  `report-metrics-2026-07-07.json` (every number below, per attempt).

## 1. THE PRE-REGISTERED GATE — verdict

| # | Gate | Bar | OFF baseline | ON (V2) | Verdict |
|---|---|---|---|---|---|
| 1 | DISCRIMINATION: strong-mean composite − misconception-mean | ≥ 0.15 | 0.025 | **0.094** | **FAIL** |
| 2 | RECALL: median strong-persona node_coverage | ≥ 0.60 | 0.633 | **1.000** | **PASS** |
| 3 | AGREEMENT vs frozen LLM band: exact / adjacent | ≥ 40% / ≥ 85% | 12.9% / 41.9% | **48.4%** (15/31) / **80.6%** (25/31) | **FAIL** (exact passes; adjacent misses by 2 attempts) |
| 4 | SAFETY (hard veto): zero misconception controls Strong AND replay per-node control-negative FCR ≤ 5% | 0 & ≤5% | — | **3 Strong** (attempts 1, 34, 35); FCR **8/10 = 80%** | **FAIL** |

**VERDICT LINE: HYPOTHESIS PARTIAL.** The recall hypothesis (the point of
V2) is confirmed decisively — the parser recall ceiling and the quadratic
edge starvation are gone, agreement with the LLM grader quadruples, zero
attempts regress, and V2 ties the frozen LLM grader against ground-truth
expected class. The composite discrimination and safety gates fail as
measured — but the §5 per-node adjudication shows the safety failures are
dominated by gold-ledger noise (the expected ledgers mislabel content the
personas provably taught inline), and the discrimination shortfall is now
bounded by the misconception PENALTY (p=0.15, findings still v1), not by
coverage. Do not promote on this evidence; follow-ups in §10.

## 2. Headline numbers — OFF vs ON per class (31 attempts)

| class | n | node_cov OFF→ON | edge_cov OFF→ON | composite OFF→ON |
|---|---|---|---|---|
| strong | 10 | 0.561 → **0.973** | 0.079 → **0.700** | 0.419 → **0.892** |
| partial | 8 | 0.462 → 0.839 | 0.063 → 0.571 | 0.345 → 0.760 |
| vague_then_clarifies | 6 | 0.631 → 0.951 | 0.063 → 0.644 | 0.459 → 0.856 |
| misconception | 7 | 0.516 → 0.873 | 0.120 → 0.637 | 0.394 → 0.798 |

Design §9 success criteria: strong-class mean node_coverage ≥ 0.6 → **0.973
PASS**; strong-vs-misconception composite separation ≥ 0.15 → 0.094 FAIL
(was 0.025); edge_coverage nonzero on ≥ 24/31 → **31/31 PASS** (was 13/31);
`control_credit_leak` ≤ baseline → **identical set PASS** (9 attempts,
unchanged — the leak metric reads the v1-binary ledger V2 does not touch);
band agreement vs LLM improves from 4/31 → **15/31 PASS**. Abstained: 0/31
in both runs (composite abstention gate active and cleared).

## 3. Calibration (DB-free sweep, §9)

- **Timing probe (card step 3):** 2.93 NLI pairs/s CPU, ~24.7 s/attempt —
  under the 90 s bar, so `TOP_K` stays 3. Full pass: 1709 unique pairs / 36
  attempts / 430 s. Sweep: 0.32 s over 972 combos (all threshold math is
  post-hoc over the cached pair scores; NLI ran ONCE per pair).
- **THE §9 CONSTRAINT IS INFEASIBLE:** recall-max ~0.92 everywhere, but FCR
  on labeled negatives is 0.636 at EVERY grid point (min = median = max) —
  entailment scores are bimodally saturated (~0.99 taught / ~0.0 absent), so
  every swept threshold lands in the same gap. The committed
  `negative_audit` block adjudicates all 22 negatives verbatim: the fired
  negatives are overwhelmingly GOLD-LABEL NOISE — the LLM personas teach the
  "omitted" content inline (e.g. the fluids misconception transcripts
  literally say "incompressible flow, which means the fluid density remains
  constant" while the ledger marks cond.incompressibility unresolved). The
  confirmed TRUE false-credit class: linear_motion eq.kinematics_position —
  the persona writes x = v0*t + a*t^2 (missing the 1/2) and NLI scores it
  0.98; entailment is blind to symbolic-coefficient errors.
- **Fitted params (documented fallback policy — max recall → min FCR → max
  strong-vs-misconception separation → nearest design defaults; the sweep
  pins t_low/t_high/alpha and is indifferent on the rest):** `t_low=0.30,
  t_mid=0.70, t_high=0.90, alpha=0.75, max_contradiction=0.30,
  lex_floor=0.10` — now the `config.py` field defaults.
  `constraint_satisfied=false` is recorded in the JSON; no fudging. Notably
  alpha=0.75 (more lexical weight) is data-pinned: with entailment
  saturated, the lexical term is what buys full-credit-tier separation.
- **Cross-subject held-out (generalization evidence):** fit on
  macroeconomics only → eval on fluid_mechanics: recall **0.910** (FCR
  0.556); fit on fluid_mechanics only → eval on macroeconomics: recall
  **0.906** (FCR 0.700). Recall transfers across subjects in both
  directions; the fitted combos differ only inside the saturation-flat
  region. linear_motion holdout numbers are in the JSON
  (`eval_linear_motion`).
- **Engine consistency:** the sweep's post-hoc math reproduces the real
  engine's node scores exactly (max diff 0.0 across all attempts). Edge
  thresholds NOT swept (no edge gold, per card); the engine-pass edge-credit
  distribution per class is in `calibration-2026-07-07.json → engine_check`.

## 4. Per-attempt (OFF nc/ec/composite band → ON, vs LLM + expected)

Bands: Str/Pro/Dev/Beg. LLM = frozen scorecard band; Exp = band of the
expected-ledger composite (formula in `report-metrics-2026-07-07.json`).

| id | persona | problem | OFF nc/ec/comp band | ON nc/ec/comp band | LLM | Exp |
|---|---|---|---|---|---|---|
| 1 | misconception | bernoulli_full_find_p2 | 0.20/0.00/0.141 Beg | 1.00/0.59/0.879 Str | Dev | Pro |
| 3 | misconception | bernoulli_horizontal_pipe_find_p2 | 0.43/0.08/0.327 Beg | 0.87/0.71/0.823 Pro | Dev | Str |
| 6 | misconception | continuity_area_change_find_v2 | 0.75/0.20/0.588 Dev | 0.75/0.40/0.647 Dev | Dev | Dev |
| 33 | misconception | gdp_identity | 0.60/0.14/0.466 Beg | 0.74/0.61/0.701 Pro | Pro | Pro |
| 34 | misconception | nnp_chain | 0.80/0.25/0.638 Dev | 1.00/0.77/0.934 Str | Str | Pro |
| 35 | misconception | real_gdp_from_deflator | 0.33/0.00/0.235 Beg | 1.00/0.85/0.956 Str | Pro | Beg |
| 36 | misconception | real_gdp_growth | 0.50/0.17/0.364 Beg | 0.75/0.53/0.649 Dev | Str | Pro |
| 7 | partial | bernoulli_full_find_p2 | 0.20/0.00/0.141 Beg | 0.92/0.69/0.853 Str | Dev | Pro |
| 8 | partial | bernoulli_height_change_find_v2 | 0.25/0.00/0.176 Beg | 0.75/0.49/0.672 Dev | Beg | Dev |
| 10 | partial | bernoulli_horizontal_pipe_find_p2 | 0.43/0.00/0.303 Beg | 0.53/0.15/0.418 Beg | Dev | Dev |
| 14 | partial | volumetric_flow_rate_find_Q | 0.50/0.00/0.353 Beg | 0.85/0.59/0.772 Pro | Beg | Beg |
| 37 | partial | gdp_identity | 0.40/0.00/0.282 Beg | 0.74/0.41/0.642 Dev | Dev | Dev |
| 38 | partial | net_exports_sign | 0.67/0.25/0.544 Dev | 1.00/0.77/0.934 Str | Str | Dev |
| 39 | partial | nnp_chain | 1.00/0.25/0.779 Pro | 1.00/0.89/0.967 Str | Str | Dev |
| 40 | partial | real_gdp_growth | 0.25/0.00/0.176 Beg | 0.93/0.58/0.825 Pro | Dev | Dev |
| 15 | strong | bernoulli_full_find_p2 | 0.20/0.00/0.141 Beg | 0.94/0.80/0.899 Str | Str | Str |
| 16 | strong | bernoulli_height_change_find_v2 | 0.75/0.14/0.572 Dev | 1.00/0.87/0.962 Str | Beg | Str |
| 18 | strong | bernoulli_horizontal_pipe_find_p2 | 0.43/0.00/0.303 Beg | 1.00/0.75/0.926 Str | Pro | Str |
| 21 | strong | continuity_area_change_find_v2 | 0.75/0.00/0.529 Dev | 0.93/0.68/0.853 Str | Dev | Str |
| 25 | strong | volumetric_flow_rate_find_Q | 0.50/0.00/0.353 Beg | 1.00/0.85/0.956 Str | Str | Str |
| 41 | strong | gdp_identity | 0.80/0.14/0.607 Dev | 0.86/0.34/0.708 Pro | Str | Str |
| 42 | strong | net_exports_sign | 0.67/0.25/0.544 Dev | 1.00/0.77/0.934 Str | Str | Str |
| 43 | strong | nnp_chain | 0.60/0.00/0.424 Beg | 1.00/0.77/0.934 Str | Str | Str |
| 44 | strong | real_gdp_from_deflator | 0.67/0.25/0.544 Dev | 1.00/0.55/0.868 Str | Str | Str |
| 45 | strong | real_gdp_growth | 0.25/0.00/0.176 Beg | 1.00/0.60/0.882 Str | Str | Str |
| 27 | vague_then_clarifies | bernoulli_height_change_find_v2 | 0.25/0.00/0.176 Beg | 0.93/0.82/0.895 Str | Pro | Pro |
| 29 | vague_then_clarifies | bernoulli_horizontal_pipe_find_p2 | 0.57/0.00/0.403 Beg | 0.86/0.28/0.686 Dev | Pro | Str |
| 47 | vague_then_clarifies | net_exports_sign | 0.67/0.00/0.471 Beg | 1.00/0.70/0.912 Str | Str | Dev |
| 48 | vague_then_clarifies | nnp_chain | 0.80/0.12/0.572 Dev | 1.00/0.85/0.926 Str | Str | Str |
| 49 | vague_then_clarifies | real_gdp_from_deflator | 1.00/0.25/0.779 Pro | 1.00/0.62/0.890 Str | Str | Str |
| 50 | vague_then_clarifies | real_gdp_growth | 0.50/0.00/0.353 Beg | 0.93/0.59/0.827 Pro | Str | Dev |

## 5. Safety adjudication (gate 4 detail — measured in the REPLAY)

Replay per-node results (V2 credit ≥ 0.7 = detection, from the ON traces):
positives **113/117 detected (96.6% recall)**; negatives 12/19 detected
(63.2%); control-persona negatives **8/10 detected (80%)** → gate FAILS as
pre-registered. Adjudication of every fired negative against its transcript
window (verbatim evidence in `calibration-2026-07-07.json → negative_audit`):

| attempt | negative key | source | adjudication |
|---|---|---|---|
| 1 | cond.incompressibility | nli | ledger noise — transcript: "incompressible flow, which means the fluid density remains constant" |
| 3 | cond.incompressibility | nli | ledger noise — teaches constant density with continuity |
| 6 | cond.incompressibility | nli | ledger noise — "fluid density remains constant or doesn't change" |
| 14 | proc.plan_apply_flow_rate_definition | nli | ledger noise — states and applies Q = A*v |
| 27 | simp.equal_pressure_simplification | nli | ledger noise — "both at atmospheric pressure ... pressure terms [cancel]" |
| 34 | def.depreciation | nli | ledger noise — defines NNP via subtracting depreciation |
| 35 | eq.gdp_deflator | nli | ledger noise — states the deflator definition and rearranges it |
| 38 | proc.subtract_imports | **v1_floor** | ledger noise — persona states "NX = X − M"; the V1 resolver itself credits it |
| 39 | proc.subtract_depreciation | nli | ledger noise — "subtract depreciation from GNP → net measure" |
| 40 | proc.apply_percent_change | nli | ledger noise — states the percent-growth formula incl. ×100 |
| 47 | cond.trade_deficit | nli | ledger noise — "negative value ... imports more than exports ... trade deficit" |
| 50 | eq.growth_rate | nli | ledger noise — walks the growth-rate formula |

**All 12 replay-fired negatives are gold-ledger noise** — the expected
ledgers describe the persona SCRIPTS (which beats were omitted), not what
the LLM role-player actually said; V1's near-zero recall previously masked
this. The one confirmed TRUE false-credit mechanism (calibration-only —
linear_motion is not replayable): NLI credits x = v0*t + a*t^2 against the
1/2-factor reference equation (misc.forgets_half_factor) — entailment
cannot see symbolic-coefficient errors, so equation-node credit needs a
non-NLI check before promotion.

The 3 misconception attempts banded Strong (1: 0.879, 34: 0.934, 35: 0.956):
their node coverage is legitimately ~1.0 (they teach 4/5 beats correctly +
the 5th inline), so the ONLY remaining discriminator is the misconception
penalty — p=0.15 × penalty(~1 asserted misc / 5 ref nodes) ≈ 0.03 of
composite, and misconception FINDINGS are still v1 (weak, the known G4
gap). Note the frozen LLM grader banded attempt 34 Strong and attempt 35
Proficient too — the corpus' misconception personas are only mildly
deficient by construction.

## 6. Agreement vs EXPECTED persona class (ground truth)

| grader | exact | adjacent |
|---|---|---|
| V2 ON | 15/31 (48.4%) | **25/31 (80.6%)** |
| frozen LLM | **16/31 (51.6%)** | 23/31 (74.2%) |
| V2 OFF (v1) | 5/31 (16.1%) | 15/31 (48.4%) |

No headline claimed: V2 TIES the LLM grader against the expected-ledger
ground truth (−1 exact, +2 adjacent) at zero LLM cost in grading — while v1
was nowhere close. (The expected-band mapping is the documented
ledger-derived formula; it inherits the same ledger noise as gate 4.)

## 7. Credit sources, budget, runtime

- **Node credit sources (136 path nodes over 31 attempts):** nli 129,
  v1_floor 6, edge_pullup 1, grayzone 0 (disabled), lexical_skip/zero 0.
  Recovery is genuinely NLI-driven; the v1 floor is nearly inert.
- **Edge evidence (engine check, 228 reference edges over 34 cases):**
  entail 91, endpoints 64, cooccur 36, none 37.
- **Pair budget:** per-attempt NLI pairs min 11 / mean 56 / max 108 of the
  200 cap — max utilization 54%, the budget never binds.
- **Wall clock:** replay OFF 6m48s; replay ON 18m00s → V2 adds ~21.7 s per
  attempt on CPU (within the design §8 25–70 s/attempt envelope).
  Calibration NLI pass 430 s total; sweep 0.32 s.

## 8. Regressions (ON < OFF)

**None.** Zero attempts have ON composite, node_coverage, or edge_coverage
below OFF (the V1-floor invariant holds live). The top-3-regressions
investigation is therefore vacuous; closest to flat: attempt 6 (+0.06
composite — its misc.pressure_rises_when_pipe_narrows contradiction veto
correctly suppresses the wrong-direction teaching) and attempt 10 (+0.11 —
the weakest ON attempt, a genuinely partial fluids teaching).

## 9. Grayzone note

Both replays ran `APOLLO_RESOLVER_V2_GRAYZONE=0` — the design card is not
ambiguous (§5 move 5 and §8 pin gray-zone OFF as the deterministic
replay/calibration default), so the ON run adds zero LLM calls. The gray
band barely populates at the fitted thresholds anyway (scores are bimodal);
grayzone's value should be re-measured live if promoted.

## 10. What this means (follow-ups, not tonight)

1. **Ledger repair before any FCR-gated decision:** re-derive per-node gold
   from the transcripts (or hand-audit the 22 negatives — the §5 table is
   the start). The corpus' control negatives are ~85% mislabeled at the
   node level.
2. **Discrimination now lives in the misconception penalty**, not coverage:
   with coverage saturated for mild-deficit personas, gate 1 needs either
   stronger misconception findings (still v1, known-weak) or a larger p /
   contradiction-aware coverage debit. Worth re-running gate 1 on personas
   with ≥ 2 omitted beats.
3. **Equation-node credit needs a symbolic check** (the 1/2-factor
   blindness) before NLI credit is trusted on eq.* nodes — e.g. keep v1's
   exact/symbolic tier as the only path to full credit for equations, or
   cap NLI-only equation credit at 0.7.
