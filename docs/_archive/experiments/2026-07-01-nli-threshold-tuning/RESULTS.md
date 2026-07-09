# Apollo NLI Resolver — Per-Model Threshold Tuning

**Date:** 2026-07-01
**Branch:** `feat/apollo-nli-resolver-tier` (tuning artifacts; no prod code changed)
**Models:** `cross-encoder/nli-deberta-v3-small` (production) and `-large`
**Stack:** local CPU, `torch 2.6.0+cpu` / `transformers 4.57.6`; both checkpoints
cached locally. Pure in-memory resolution (no DB/Docker needed).

Follows the 2026-06-30 probe (`../2026-06-30-nli-grading-probe/RESULTS.md`),
which recommended (§8/§11.6): expand the dev set with corpus pairs, tune
`min_entailment` per model, and re-tune the veto. This does all three.

---

## TL;DR

Tuned **separately per model** against an expanded, corpus-faithful dev set
(104 reference pairs + 24 veto records). Both models hit **precision 1.000 with
zero false credits** end-to-end; they differ sharply in *where* the recall lives.

| | **small** (prod) | **large** |
|---|---|---|
| `min_entailment` | **0.70** robust / 0.50 max-recall | **0.90** |
| `max_contradiction` | 0.10 | 0.05 |
| `misconception_veto_entailment` | **0.96** | **0.98** |
| reference precision (dev) | 1.000 | 1.000 |
| reference recall (pair-level) | 0.607 @0.70 · 0.661 @0.50 | **0.714** (flat 0.50→0.90) |
| reference recall (end-to-end via NLI) | 33 / 56 | **36 / 56** |
| veto recall / precision | 0.917 / 1.000 | 0.833 / 1.000 |
| Layer-2 false credits | **0** | **0** |
| domain paraphrase "steady flow" (§smoke) | MISS (0.31) | **HIT (0.93)** |

**Three headline findings:**

1. **The current production veto threshold `0.80` is unsafe.** On *both* models a
   correct student statement false-entails a misconception surface at
   **0.88–0.98** (an NLI polarity/lexical-overlap error). At `0.80` those would
   fire as **false vetoes** — denying credit to correct students. The veto floor
   must rise to **0.96 (small) / 0.98 (large)**. This is a safety fix
   independent of any recall tuning.
2. **The current `min_entailment=0.87` is over-conservative for the small model.**
   Precision is 1.000 for *every* threshold down to 0.50 (the model puts zero
   negatives above ent≈0.00), so 0.87 forfeits recall for no precision gain:
   0.500 → 0.607 (@0.70) → 0.661 (@0.50).
3. **The large model is strictly better shaped.** Its recovered positives all
   score ≥0.90 (confident, bimodal), so recall is **flat at 0.714 from 0.50 to
   0.90** — more recall than small, at a far more robust threshold, plus it
   catches domain paraphrases small misses. A model swap is the highest-leverage
   upgrade, and — unlike RESULTS §11.1 warned for the *old untuned* threshold —
   it does **not** lose recoveries once the threshold is re-tuned to 0.90.

---

## 1. Method — "same as the probe, and more"

The 2026-06-30 probe authored a handful of paraphrase pairs per node and read the
raw model scores. This scales that to the full corpus and adds a formal,
per-model precision/recall sweep with a precision-first operating-point rule and
an end-to-end false-credit gate.

### 1.1 Expanded dev set (`scripts/nli_tuning_build_dev_set.py`)

Authored student premises are keyed by `entity_key`; **hypotheses are pulled from
the live problem JSON at build time**, so every hypothesis is byte-identical to
the production surface the NLI tier sees (`content.label`, verified wired through
`candidates_from_reference_solution → candidate_surface_texts → match_nli_semantic`).

- **56 positives** — 2 faithful paraphrases (one clean, one indirect) for each of
  the 28 NLI-eligible reference steps across all 10 corpus problems. `gold=entailment`.
- **48 hard negatives** — all **type-faithful** (the resolver's `type_compatible`
  is strict same-type, so cross-type pairs can never compete in production):
  - 36 **within-problem same-type sibling** pairs (production's real shortlist
    competition — all `procedure_step`×`procedure_step` here).
  - 12 **curated same-type, known-distinct** cross-group pairs, giving the
    singleton conditions/definitions/simplifications negative coverage
    (`final_goods_only`↔`trade_deficit`, `horizontal`↔`equal_pressure`,
    `depreciation`↔`real_basis`). `gold=neutral`.
- **24 veto records** — per misconception: 2 misconception-voicing premises
  (should fire) + 2 correct-opposite premises (must **not** fire).

Genuine near-duplicate steps that repeat across problems (the continuity steps in
bernoulli p01/p03/p05) are deliberately **not** cross-paired as negatives — a
paraphrase of one legitimately entails the other, so pairing them would be a
mislabeled negative.

### 1.2 Reference sweep (`scripts/nli_tuning_sweep.py`)

Pair-level, which is **faithful for references** because production reference
candidates expose a single surface (the label) — the shortlist has nothing else
to pick. Reuses the tested production apparatus
(`calibration.sweep_thresholds` + `best_operating_point(min_precision=0.95)`) and
adds a **fine grid** (`min_ent` 0.50→0.99 step 0.01 × `max_con`
{0.02,0.05,0.10,0.15,0.20,0.30,∞}). Operating-point rule = **precision ≥ 0.95
first, then max recall**, tie-broken to the *highest* threshold holding that
recall (maximal FP headroom).

### 1.3 Veto sweep (Layer-2 surface selection)

The veto shortlists over `display_name` + every `trigger_phrase`, so pair-level is
*not* faithful. For each veto record we build the concept's misconception
candidate set and take the **max entailment over all misconception surfaces that
pass the production polarity gate** — an embedder-independent, conservative upper
bound on veto firing. Sweep `misconception_veto_entailment`; pick max veto-recall
subject to **zero false vetoes**.

### 1.4 End-to-end gate (`scripts/nli_tuning_gate.py`)

Drives the **full** `resolve_attempt` (polarity → content-overlap floor →
semantic shortlist → NLI certify → ambiguity margin → misconception veto →
competition) with each chosen config over every positive and misconception
premise, plus the RESULTS §2 smoke pairs. A config is eligible for default-ON
only if **false credits == 0** here.

---

## 2. Reference-credit sweep

**Both models: precision 1.000, and the highest negative entailment under the
contradiction cap is 0.000** — i.e. neither model places *any* negative near the
positives. Precision is not the binding constraint; recall is. Full per-cell grids, complete recall-miss lists, and the 24-row veto table are in §7–§9.

### 2.1 small — recall rises as the threshold drops

| `min_ent` (max_con 0.10) | recall | precision |
|---|---|---|
| 0.87 (current prod) | 0.500 (28/56) | 1.000 |
| 0.80 | 0.500 | 1.000 |
| 0.70 | **0.607 (34/56)** | 1.000 |
| 0.60 | 0.607 | 1.000 |
| 0.50 | **0.661 (37/56)** | 1.000 |

0.60 and 0.70 tie on recall → **0.70 is the robust pick** (same recall, more
headroom, +6 recoveries vs prod). 0.50 adds 3 more (ent∈[0.50,0.60) — the weak
tail RESULTS §11.6 assigns to clarification). The 19 misses are dominated by
procedure steps the small model can't entail; several it actively *contradicts*
at con≈0.96 (`cond.final_goods_only`, `def.real_basis`, `proc.compute_net_exports`).

### 2.2 large — recall flat and high, threshold robust

| `min_ent` (max_con 0.10) | recall | precision |
|---|---|---|
| 0.50 → 0.90 | **0.714 (40/56)** | 1.000 |
| 0.95 | 0.679 (38/56) | 1.000 |

Every positive the large model recovers scores ≥0.90, so recall is invariant to
the threshold across 0.50–0.90. **`min_entailment=0.90` gives max recall AND
maximal robustness.** The fine grid's `max_con=0.02` is incidental (negatives sit
at con≈0); `0.05` is used for a small production-consistent margin with no recall
change.

---

## 3. Veto sweep

The dev set surfaced a real **false-veto tendency** in both models: a *correct*
statement entailing a misconception surface at high confidence.

| model | selected veto floor | veto recall | false vetoes | max false-veto seen |
|---|---|---|---|---|
| small | **0.96** | 0.917 (11/12) | 0 | 0.953 (correct deflation ⊨ "Uses nominal as real") |
| large | **0.98** | 0.833 (10/12) | 0 | 0.979 (correct tradeoff ⊨ "Pressure and velocity move together") |

The chosen floors sit just above each model's worst false-veto. Consequence: the
current default `0.80` would fire **2 false vetoes on both models** on this dev
set. The 1–2 missed true misconceptions at the higher floor are the *softest*
paraphrases; a missed veto is safe — it leaves the node **unresolved** (not
credited), never a false credit (confirmed in §4), and is the correct hand-off to
the clarification loop.

---

## 4. End-to-end false-credit gate

Full `resolve_attempt` with each chosen config:

| config | positives | recovered via NLI | veto cases not-credited | **false credits** | verdict |
|---|---|---|---|---|---|
| small `min_ent=0.50, max_con=0.10, veto=0.96` | 56 | 33 | 40/40 | **0** | PASS |
| large `min_ent=0.90, max_con=0.05, veto=0.98` | 56 | 36 | 40/40 | **0** | PASS |

Smoke (RESULTS §2): both nail the clear paraphrase (0.94 / 0.99 entailment) and
the inverse-relationship contradiction (0.999), and neither credits the unrelated
pair. Only the **large** model also entails the indirect "flow does not change over
time" ⊨ "steady flow" (0.93 vs small's 0.31) — a genuine recall edge.

Both configs are eligible for default-ON by the calibration gate.

---

## 5. Recommendation for hooking into the grader

`min_entailment` / `max_contradiction` / `misconception_veto_entailment` are all
env-overridable today (`APOLLO_NLI_MIN_ENTAILMENT`, `APOLLO_NLI_MAX_CONTRADICTION`,
`APOLLO_NLI_MISC_VETO_ENT`), so per-model rollout needs no code change — only
config. Defaults in `nli_config.NLIParams` are model-agnostic (0.87 / 0.10 / 0.80).

1. **Safety first (do regardless of model): raise the veto floor.** `0.80` is
   unsafe. Set **0.96** (small) or **0.98** (large). This is the one change that
   fixes an active false-veto class, not just recall.
2. **If staying on the small model:** `min_entailment=0.70`, `max_contradiction=0.10`,
   veto `0.96`. Recall 0.500 → 0.607 at precision 1.000. (Use 0.50 to push recall
   to 0.661 if you accept crediting the ent∈[0.50,0.60) tail instead of routing it
   to clarification.)
3. **Preferred: switch to `-large`** with `min_entailment=0.90`,
   `max_contradiction=0.05`, veto `0.98`. Higher recall (0.714) at a robust
   threshold, and it recognizes domain paraphrases the small model misses. Cost:
   larger checkpoint / more CPU per call — weigh against the recall + robustness
   gain. **Do not swap without these tuned thresholds** — the untuned 0.87 floor
   under-scores the large model (RESULTS §11.1).

To bake per-model defaults into `NLIParams` (rather than env), that is a small
scoped code change under the 95% patch-coverage gate + owner-doc reconcile.

## 6. Honesty / limits

- **Dev set is authored, not real student data.** It is corpus-faithful and the
  precision margin is total (max negative ent = 0.000), but distribution shift on
  live attempts could differ. Expand with real graded attempts when available
  (RESULTS §8 rec 4) and re-run these three scripts unchanged.
- **Veto evaluation uses max-over-surfaces**, not the exact production shortlist
  surface pick. This is a *conservative* upper bound on veto firing (it finds any
  surface that could fire), so the chosen floors are if anything slightly strict —
  the safe direction.
- Reference sweep is pair-level; faithfulness rests on reference candidates having
  a single surface (the label). The §4 end-to-end gate through the real resolver
  confirms the pair-level conclusions hold with all guards active.

## 7. Full fine-grid detail

Complete cell-by-cell grids are in `fine_grid_<model>.csv` (50 `min_ent` × 7 `max_con` = 350 rows each). Because **precision is 1.000 at every cell** and the highest negative entailment is 0.000, recall is a step function of `min_entailment` alone; the tables below give the lossless recall breakpoints at `max_con=0.1` (fp=0 throughout).

### 7.1 small — recall breakpoints (`max_con=0.1`)

| `min_ent` range | recall | tp/56 |
|---|---|---|
| 0.50 | 0.661 | 37 |
| 0.51–0.58 | 0.643 | 36 |
| 0.59–0.70 | 0.607 | 34 |
| 0.71–0.74 | 0.554 | 31 |
| 0.75–0.79 | 0.518 | 29 |
| 0.80–0.88 | 0.500 | 28 |
| 0.89 | 0.482 | 27 |
| 0.90–0.92 | 0.464 | 26 |
| 0.93–0.95 | 0.446 | 25 |
| 0.96 | 0.411 | 23 |
| 0.97 | 0.375 | 21 |
| 0.98 | 0.304 | 17 |
| 0.99 | 0.179 | 10 |

### 7.2 large — recall breakpoints (`max_con=0.1`)

| `min_ent` range | recall | tp/56 |
|---|---|---|
| 0.50–0.91 | 0.714 | 40 |
| 0.92–0.94 | 0.696 | 39 |
| 0.95–0.96 | 0.679 | 38 |
| 0.97 | 0.661 | 37 |
| 0.98 | 0.536 | 30 |
| 0.99 | 0.446 | 25 |

### 7.3 `max_contradiction` sensitivity (at each model's selected `min_ent`)

| model | `min_ent` | `max_con` | recall | precision |
|---|---|---|---|---|
| small | 0.50 | 0.02 | 0.589 | 1.000 |
| small | 0.50 | 0.05 | 0.607 | 1.000 |
| small | 0.50 | 0.10 | 0.661 | 1.000 |
| small | 0.50 | 0.15 | 0.661 | 1.000 |
| small | 0.50 | 0.20 | 0.661 | 1.000 |
| small | 0.50 | 0.30 | 0.661 | 1.000 |
| small | 0.50 | ∞ | 0.661 | 1.000 |
| large | 0.90 | 0.02 | 0.714 | 1.000 |
| large | 0.90 | 0.05 | 0.714 | 1.000 |
| large | 0.90 | 0.10 | 0.714 | 1.000 |
| large | 0.90 | 0.15 | 0.714 | 1.000 |
| large | 0.90 | 0.20 | 0.714 | 1.000 |
| large | 0.90 | 0.30 | 0.714 | 1.000 |
| large | 0.90 | ∞ | 0.714 | 1.000 |

`max_con` behaves differently per model. For **large** it is inert — recall is 0.714 at every cap (its recovered positives all have con≈0). For **small** it is a live knob *below* 0.10: tightening to 0.05 / 0.02 drops recall to 0.607 / 0.589 (a few correct paraphrases carry contradiction up to ~0.10), while loosening above 0.10 adds nothing (no negative is ever labeled entailment). So `max_con=0.10` is the small-model plateau knee — the loosest cap that still admits zero negatives — and 0.05 costs the large model no recall.

## 8. Complete recall-miss lists

Every positive NOT credited at each model's **max-recall** operating point (`min_ent` 0.50 small / 0.90 large, `max_con=0.10`). These are the model's capability ceiling — no in-range threshold recovers them; the `contradiction` cases are where the model actively mis-reads a correct paraphrase.

### 8.1 small — 19/56 missed (at `min_ent=0.50`)

| node | type | ent | con | label |
|---|---|---|---|---|
| `cond.final_goods_only` | condition | 0.005 | 0.968 | contradiction |
| `def.real_basis` | definition | 0.007 | 0.965 | contradiction |
| `proc.apply_percent_change` | procedure_step | 0.043 | 0.002 | neutral |
| `proc.compute_net_exports` | procedure_step | 0.009 | 0.964 | contradiction |
| `proc.plan_apply_continuity` | procedure_step | 0.034 | 0.357 | neutral |
| `proc.plan_apply_continuity_for_v2` | procedure_step | 0.036 | 0.489 | contradiction |
| `proc.plan_apply_equal_pressure_simplification` | procedure_step | 0.001 | 0.000 | neutral |
| `proc.plan_apply_horizontal_simplification` | procedure_step | 0.059 | 0.192 | neutral |
| `proc.plan_invoke_incompressibility` | procedure_step | 0.336 | 0.202 | neutral |
| `proc.plan_set_v1_zero_and_solve_bernoulli` | procedure_step | 0.001 | 0.919 | contradiction |
| `proc.plan_solve_bernoulli_for_p2` | procedure_step | 0.245 | 0.002 | neutral |
| `proc.plan_solve_continuity_for_v2` | procedure_step | 0.008 | 0.965 | contradiction |
| `proc.plan_solve_continuity_for_v2` | procedure_step | 0.002 | 0.006 | neutral |
| `proc.plan_substitute_into_bernoulli` | procedure_step | 0.000 | 0.000 | neutral |
| `proc.rearrange_for_real_gdp` | procedure_step | 0.048 | 0.095 | neutral |
| `proc.subtract_depreciation` | procedure_step | 0.002 | 0.001 | neutral |
| `proc.subtract_imports` | procedure_step | 0.005 | 0.000 | neutral |
| `proc.sum_components` | procedure_step | 0.030 | 0.001 | neutral |
| `simp.equal_pressure_simplification` | simplification | 0.008 | 0.005 | neutral |

### 8.2 large — 16/56 missed (at `min_ent=0.90`)

| node | type | ent | con | label |
|---|---|---|---|---|
| `def.real_basis` | definition | 0.000 | 0.000 | neutral |
| `proc.compute_net_exports` | procedure_step | 0.004 | 0.000 | neutral |
| `proc.plan_apply_continuity` | procedure_step | 0.001 | 0.985 | contradiction |
| `proc.plan_apply_continuity_for_v2` | procedure_step | 0.002 | 0.949 | contradiction |
| `proc.plan_apply_equal_pressure_simplification` | procedure_step | 0.000 | 0.000 | neutral |
| `proc.plan_apply_horizontal_simplification` | procedure_step | 0.000 | 0.996 | contradiction |
| `proc.plan_invoke_incompressibility` | procedure_step | 0.007 | 0.007 | neutral |
| `proc.plan_set_v1_zero_and_solve_bernoulli` | procedure_step | 0.001 | 0.000 | neutral |
| `proc.plan_solve_continuity_for_v2` | procedure_step | 0.159 | 0.005 | neutral |
| `proc.plan_substitute_into_bernoulli` | procedure_step | 0.000 | 0.000 | neutral |
| `proc.rearrange_for_real_gdp` | procedure_step | 0.001 | 0.000 | neutral |
| `proc.subtract_depreciation` | procedure_step | 0.079 | 0.000 | neutral |
| `proc.subtract_imports` | procedure_step | 0.000 | 0.000 | neutral |
| `simp.deflator_is_price_index` | simplification | 0.072 | 0.000 | neutral |
| `simp.equal_pressure_simplification` | simplification | 0.000 | 0.000 | neutral |
| `simp.horizontal_simplification` | simplification | 0.026 | 0.000 | neutral |

## 9. Full veto record table

All 24 veto records with each model's **max entailment over the concept's misconception surfaces** (polarity-gated). `fires?` is at the selected floor (small 0.96 / large 0.98). A `veto_positive` should fire; a `veto_negative` must not. Rows are joined by construction order (same dev set).

| kind | misconception | small ent | small fires? | large ent | large fires? | best surface (large) |
|---|---|---|---|---|---|---|
| POS | `misc.pressure_velocity_same_direction` | 0.996 | ✓ | 0.999 | ✓ | pressure goes up when it speeds up |
| POS | `misc.pressure_velocity_same_direction` | 0.996 | ✓ | 0.999 | ✓ | pressure goes up when it speeds up |
| neg | `misc.pressure_velocity_same_direction` | 0.883 | ok | 0.979 | ok | Pressure and velocity move together |
| neg | `misc.pressure_velocity_same_direction` | 0.000 | ok | 0.000 | ok | — |
| POS | `misc.density_ignored` | 0.991 | ✓ | 0.981 | ✓ | ignore density |
| POS | `misc.density_ignored` | 0.985 | ✓ | 0.982 | ✓ | density doesn't matter |
| neg | `misc.density_ignored` | 0.000 | ok | 0.000 | ok | — |
| neg | `misc.density_ignored` | 0.000 | ok | 0.000 | ok | — |
| POS | `misc.includes_transfers` | 0.986 | ✓ | 0.996 | ✓ | count used cars in gdp |
| POS | `misc.includes_transfers` | 0.991 | ✓ | 0.997 | ✓ | transfer payments count |
| neg | `misc.includes_transfers` | 0.000 | ok | 0.000 | ok | — |
| neg | `misc.includes_transfers` | 0.000 | ok | 0.000 | ok | — |
| POS | `misc.gross_for_net` | 0.989 | ✓ | 0.991 | ✓ | no need to subtract depreciation |
| POS | `misc.gross_for_net` | 0.984 | ✓ | 0.997 | ✓ | Uses gross instead of net (forgets depreci |
| neg | `misc.gross_for_net` | 0.000 | ok | 0.000 | ok | — |
| neg | `misc.gross_for_net` | 0.000 | ok | 0.000 | ok | — |
| POS | `misc.nominal_for_real` | 0.976 | ✓ | 0.992 | ✓ | no need to adjust for inflation |
| POS | `misc.nominal_for_real` | 0.820 | — | 0.938 | — | no need to adjust for inflation |
| neg | `misc.nominal_for_real` | 0.000 | ok | 0.000 | ok | — |
| neg | `misc.nominal_for_real` | 0.000 | ok | 0.000 | ok | — |
| POS | `misc.deflate_wrong_direction` | 0.992 | ✓ | 0.968 | — | multiply nominal gdp by the price index |
| POS | `misc.deflate_wrong_direction` | 0.960 | ✓ | 0.985 | ✓ | multiply by the deflator |
| neg | `misc.deflate_wrong_direction` | 0.953 | ok | 0.958 | ok | Uses nominal GDP as real GDP |
| neg | `misc.deflate_wrong_direction` | 0.000 | ok | 0.000 | ok | — |

Note the `neg` rows scoring 0.88–0.98 (e.g. a correct deflation statement entailing "Uses nominal GDP as real GDP"): these are the false-veto risks the high floors are calibrated to clear — the model's polarity/lexical-overlap errors, not a dev-set labeling issue.

---

## Appendix — artifacts (this directory)

- `dev_set_reference.jsonl` — 104 pair-level reference pairs (premise/hypothesis/gold/kind).
- `dev_set_veto.json` — 24 veto records.
- `tuning_results_nli-deberta-v3-small.json`, `tuning_results_nli-deberta-v3-large.json`
  — full grids + per-pair raw scores + selected operating points.
- `fine_grid_nli-deberta-v3-small.csv`, `fine_grid_nli-deberta-v3-large.csv`
  — the complete 350-cell (`min_ent` × `max_con`) sweep per model.
- Scripts (in `scripts/`): `nli_tuning_build_dev_set.py`, `nli_tuning_sweep.py`,
  `nli_tuning_analyze.py`, `nli_tuning_gate.py`.
