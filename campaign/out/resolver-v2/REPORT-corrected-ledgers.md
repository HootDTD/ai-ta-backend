# Resolver V2 — gate recomputation on quote-audited corrected ledgers (2026-07-07, ledger repair)

- **Follows:** `REPORT.md` §5/§10.1 (same branch `feat/apollo-resolver-v2`, base
  4230a0b). REPORT.md's safety gate failed with control-negative FCR 8/10 = 80%,
  but its own §5 adjudication showed the fired negatives were dominated by
  gold-ledger noise: the expected ledgers describe the persona SCRIPTS (which
  beats were supposed to be omitted), while the LLM role-players taught the
  "omitted" content inline. This report repairs the measurement — data
  adjudication + report math only; **zero code changes** (`apollo/**`
  untouched), **`campaign/out/f1c/**` untouched** (corrections live in the new
  `corrected-negatives-2026-07-07.json`).
- **Method:** every one of the 22 calibration per-node negatives (the full
  `negative_audit` set — 12 control-persona + 10 partial-persona; 19 of the 22
  are in the replay, the 3 linear_motion ones are calibration-only) was
  re-adjudicated against its **full transcript** in
  `campaign/out/f1c/attempts.jsonl`, not just the NLI window. A negative flips
  to positive only when a verbatim quote unambiguously teaches the node's
  substantive content; a related word or passing mention is not teaching.
  Genuinely arguable cases are marked **borderline** and every gate is computed
  BOTH ways: **treatment A** = borderlines-as-negative, **treatment B** =
  borderlines-as-positive.
- **Honesty note:** these corrections are quote-grounded and human-auditable
  (every row below cites verbatim transcript text), but they were made by an
  LLM (Claude), not a human rater. The borderline class bounds the judgment
  calls; a human pass over the 22 rows is cheap and should precede any
  promotion decision that leans on the corrected FCR.
- **Recompute validation:** the documented expected-composite formula
  (`report-metrics-2026-07-07.json → expected_band_formula`, reference
  structure rebuilt via `build_reference_canonical` on the same problem
  enumeration as `scripts/resolver_v2_calibrate.py`) reproduces all 31
  original `expected_composite` values, max abs diff 5.0e-05 (rounding).

## 1. Correction table — all 22 decisions

**Tally: 7 flipped-positive / 11 stays-negative / 4 borderline.** Full verbatim
quotes + per-row justifications in `corrected-negatives-2026-07-07.json`
(audit_index preserves `calibration-2026-07-07.json → negative_audit` order).
"Fired" = V2 credit ≥ 0.7 in the ON replay (calib-only rows: in calibration).

| ai | attempt | node key | ctl | fired | verdict | quote (abridged) — justification |
|---|---|---|---|---|---|---|
| 0 | 50 vague | eq.growth_rate | Y | Y | **FLIP** | "divide it by the earlier year's GDP figure … multiply the result by 100. This final figure will give us the total percentage growth in real GDP" — persona clarified when Apollo asked (the vague persona's designed behavior) and walked the full formula. |
| 1 | 27 vague | simp.equal_pressure_simplification | Y | Y | **FLIP** | "both at atmospheric pressure … these pressure terms cancel each other out when we set up Bernoulli's equation" — the exact simplification, taught explicitly (and re-used as P1=P2 in the worked solve). |
| 2 | 35 misc | eq.gdp_deflator | Y | Y | **FLIP** | "Real GDP = Nominal GDP / GDP Deflator × 100 … the deflator will be above 100 [if prices increased]" + "shows how much prices have changed relative to the base year" — definition + equation taught correctly in turns 0–2; the turn-4 multiply-instead-of-divide is the separate, ledgered misc.deflate_wrong_direction. |
| 3 | 1 misc | cond.incompressibility | Y | Y | **FLIP** | "incompressible flow, which means the fluid density remains constant" — the node's content verbatim, turn 0. |
| 4 | 47 vague | cond.trade_deficit | Y | Y | **FLIP** | "A negative value, such as −20, indicates that the economy imports more … than it exports. This is what's known as a trade deficit. Conversely … a trade surplus" — explicit, with the converse. |
| 5 | 39 partial | proc.subtract_depreciation | N | Y | *borderline* | "to compute NNP, you subtract the depreciation from the GNP" (×2) — instruction stated verbatim in problem context but never executed (562−40 never performed; sibling attempt 34 was credited only after executing). Stated-not-executed class. |
| 6 | 34 misc | def.depreciation | Y | Y | **FLIP** | "DEP stands for Depreciation, which is the loss in value of capital assets over the year … wear and tear" — defined explicitly AND its subtract-from-gross role executed (562−40=522). |
| 7 | 38 partial | proc.subtract_imports | N | Y | *borderline* | "exports minus … imports. NX = X − M … replace the symbols with the specific numbers" — operation stated, substitution delegated, 100−120 never executed by the teacher. (The incumbent V1 resolver itself credits this node — v1_floor.) |
| 8 | 6 misc | cond.incompressibility | Y | Y | **FLIP** | "incompressible … essentially means the fluid density remains constant or doesn't change" — explicit definition, in direct answer to Apollo's "define incompressible in your own words". |
| 9 | 14 partial | proc.plan_apply_flow_rate_definition | N | Y | **STAYS** | Single definitional turn ("Q = A * v") — no plan, no application to the problem's numbers; the plan node's differential content vs the credited eq.flow_rate_definition is absent. **TRUE false credit** (plan-credit-from-bare-definition). Corrects T8's audit note, which claimed "states and applies" — it never applies. |
| 10 | 40 partial | proc.apply_percent_change | N | Y | *borderline* | Formula "((Final−Initial)/Initial) × 100" taught with the problem's values named, but the persona stops after the absolute-change setup; the percent step is never performed or directed as an action. Stated-not-executed class. |
| 11 | cyclist misc | proc.compute_distance | Y | calib | **STAYS** | "x = v0·t + a·t² … x = 2.0 · 25.0 = 50.0 meters" — substitutes into an equation **missing the ½ factor**, wrong answer (correct 25.0 m). **TRUE false credit** (NLI coefficient blindness); calibration-only. |
| 12 | cyclist misc | eq.kinematics_position | Y | calib | **STAYS** | Same quote — the taught equation is wrong (scripted misc.forgets_half_factor), so the node was not taught. **TRUE false credit**; calibration-only. |
| 13 | 3 misc | cond.incompressibility | Y | Y | *borderline* | "for an incompressible fluid … mass flow rate must remain constant … A₁v₁ = A₂v₂" — incompressibility is named as the condition gating continuity, but unlike attempts 1/6 the constant-density content is never stated. Condition named without its meaning. |
| 14 | 7 partial | proc.plan_substitute_into_bernoulli | N | N | **STAYS** | No quote exists — transcript ends after continuity yields V2=6 m/s; Bernoulli substitution never planned/performed. Label correct; V2 correctly silent (ent 0.16). |
| 15 | 8 partial | proc.plan_set_v1_zero_and_solve_bernoulli | N | N | **STAYS** | v1≈0 and the solve never taught. Label correct; V2 correctly silent. |
| 16 | 37 partial | proc.sum_components | N | N | **STAYS** | "GDP is calculated by summing up these components" is part of the credited identity; the sum over this problem's values is never planned/executed. Label correct; V2 correctly silent. |
| 17 | 10 partial | proc.plan_apply_horizontal_simplification | N | N | **STAYS** | Simplification concept credited separately (simp.horizontal_simplification); applying it in a solve-for-P2 plan never happens (transcript ends after v2=4.0). Label correct; V2 correctly silent. |
| 18 | 10 partial | proc.plan_solve_bernoulli_for_p2 | N | N | **STAYS** | No P2 work anywhere. Label correct; V2 correctly silent. |
| 19 | cyclist partial | proc.compute_distance | N | N | **STAYS** | Position equation taught correctly (and credited) but distance never computed. Label correct; V2 correctly silent; calibration-only. |
| 20 | 33 misc | cond.final_goods_only | Y | N | **STAYS** | Never taught — "final goods and services" only in passing; turn 8 teaches the OPPOSITE (transfers + used goods included, the scripted misconception). Genuine surviving true negative. |
| 21 | 36 misc | def.real_basis | Y | N | **STAYS** | Never taught; turn 6 teaches the OPPOSITE ("nominal GDP is essentially the same as real GDP"). Genuine surviving true negative. |

**Pattern:** all 7 flips are misconception/vague-persona rows where the LLM
role-player taught the scripted-omitted beat inline (5 of 7 in direct response
to an Apollo clarification question — the personas answer questions honestly
even about content their script omits). All 4 borderlines are one class:
the operation is *stated* in the problem's context but never *executed* on
the problem's numbers. All 3 confirmed TRUE false credits are one of two
mechanisms: plan-credit-from-bare-definition (ai 9) and symbolic-coefficient
blindness (ai 11/12, calibration-only).

## 2. Recomputed pre-registered gate table (corrected labels)

Gates 1–3 do not read the per-node negative labels — recomputed anyway,
identical to REPORT.md. Gate 4 is where the repair bites. A = borderlines-as-
negative, B = borderlines-as-positive.

| # | Gate | Bar | REPORT.md (as-measured) | Corrected, treatment A | Corrected, treatment B | Verdict |
|---|---|---|---|---|---|---|
| 1 | DISCRIMINATION: strong-mean − misconception-mean composite (ON) | ≥ 0.15 | 0.094 | 0.094 | 0.094 | **FAIL** (unchanged — see §4) |
| 2 | RECALL: median strong node_coverage (ON) | ≥ 0.60 | 1.000 | 1.000 | 1.000 | **PASS** |
| 3 | AGREEMENT vs frozen LLM band: exact / adjacent | ≥ 40% / ≥ 85% | 48.4% / 80.6% | 48.4% / 80.6% | 48.4% / 80.6% | **FAIL** (exact passes; adjacent misses by 2 attempts) |
| 4a | SAFETY: zero misconception-control attempts banded Strong | 0 | 3 (attempts 1, 34, 35) | 3 — all reclassified **MISCONCEPTION-PENALTY failures**, 0 coverage false-credits (§3) | same | **FAIL as written**; failure locus moves out of the resolver |
| 4b | SAFETY: replay control-negative FCR | ≤ 5% | 8/10 = **80%** | **1/3 = 33.3%** → FAIL | **0/2 = 0%** → PASS | **UNDETERMINED** — see below |
| — | (info) replay all-negatives FCR | — | 12/19 = 63.2% | 5/12 = 41.7% | 1/8 = 12.5% | — |
| — | (info) replay positive recall | — | 113/117 = 96.6% | 120/124 = 96.8% | 124/128 = 96.9% | — |

**Gate 4b honest reading:** the corrected control-negative bank has **2–3
surviving true negatives** (attempts 33, 36, and borderline 3). A pass at
0/2 or a fail at 1/3 are both statistically meaningless against a 5% bar —
after ledger repair the corpus simply **cannot measure control FCR**. The one
control fire that survives treatment A (attempt 3, cond.incompressibility) is
the borderline condition-named-without-meaning case. What CAN be said: of the
12 replay-fired negatives, after repair only **1 unambiguous false credit
survives** (attempt 14, plan-from-definition, a *partial*-persona negative),
vs 8/10 control + 12/19 overall as originally scored — the 80% headline FCR
was overwhelmingly a gold-label artifact, and the two confirmed false-credit
mechanisms are both structural (plan-vs-definition type confusion, symbolic
coefficients), not threshold problems.

## 3. Gate 4a — the 3 misconception attempts banded Strong, classified

Under corrected ledgers, all three have **legitimately** full node coverage —
the coverage credit is CORRECT, so none is a coverage false-credit. Each
persona also asserted its scripted misconception, which the composite failed
to materially penalize (penalty term p=0.15 × ~1 misc/path ≈ 0.03–0.05 of
composite; misconception FINDINGS still v1, the known-weak G4 gap). All three
are therefore **MISCONCEPTION-PENALTY failures**:

| attempt | corrected coverage | asserted misconception (quote) | classification |
|---|---|---|---|
| 1 (0.879 Str) | 5/5 legit (incompressibility flip) | "you can actually ignore the density of the fluid completely" (turn 8 — flatly wrong physics) | misconception-penalty failure |
| 34 (0.934 Str) | 5/5 legit (def.depreciation flip) | "GNP essentially equals NNP … without needing to make additional adjustments like for depreciation" (turn 8) | misconception-penalty failure (frozen LLM also banded Strong) |
| 35 (0.956 Str) | 3/3 legit (eq.gdp_deflator flip) | "multiply the nominal GDP by the GDP deflator … take the 543.3 billion and multiply it by 19.0" (turn 4 — answer wrong by ~10⁴×) | misconception-penalty failure (frozen LLM banded Proficient) |

The pre-registered clause fails as written (3 Strong ≠ 0), but the blocker it
exposes is the **penalty/findings subsystem**, not V2 coverage: a persona that
teaches 5/5 beats correctly AND asserts a serious misconception *should* lose
its band to the penalty, and currently cannot.

## 4. Expected-class agreement on corrected ledgers (note only)

Persona-level classes are unchanged. The ledger-derived expected BANDS move
for 7 attempts (A) / 10 (B) — all upward, all to Strong, **including the three
misconception controls** (e.g. attempt 35: Beginning → Strong at 0.95). That
is the formula faithfully reporting that its penalty term (0.15·|misc|/|path|)
cannot represent "taught a serious misconception" — corroborating §3. The
misconception≠Strong invariant must be enforced at persona level (as gate 4a
does), not through this formula. With that caveat, agreement vs the corrected
expected bands:

| grader | exact (A) | adjacent (A) | exact (B) | adjacent (B) |
|---|---|---|---|---|
| V2 ON | **19/31 (61.3%)** | **26/31 (83.9%)** | **21/31 (67.7%)** | **28/31 (90.3%)** |
| frozen LLM | 17/31 (54.8%) | 24/31 (77.4%) | 18/31 (58.1%) | 25/31 (80.6%) |

V2 goes from tying the frozen LLM grader (REPORT.md §6, original ledgers) to
**beating it under both treatments** — at zero LLM cost in grading.

## 5. VERDICT LINE

**On corrected ledgers: G2 PASS (1.000), G3-exact PASS (48.4%), G3-adjacent
FAIL by 2 attempts (80.6% vs 85%), G1 FAIL (0.094 vs 0.15 — unchanged), G4a
FAIL as written but with all 3 violations reclassified as misconception-penalty
failures (zero V2 coverage false-credits), G4b control FCR 33.3% (A) / 0% (B)
on a 3/2-negative denominator — the corpus no longer has the power to decide
the ≤5% safety bar.** The 80% FCR headline that failed REPORT.md's safety gate
is a measurement artifact of script-derived gold: after quote-audited repair,
exactly ONE unambiguous replay false credit survives (attempt 14). Do not
promote on this evidence either — but the remaining TRUE blockers are now
correctly located:

1. **Misconception penalty, not coverage** (G1 + G4a): findings are still v1
   and p=0.15 contributes ~0.03 of composite; mild-deficit personas saturate
   coverage, so discrimination and the misconception≠Strong invariant both
   live in the penalty term. (REPORT.md §10.2.)
2. **Two structural false-credit mechanisms in V2 crediting** (survive repair):
   NLI plan-credit-from-bare-definition (attempt 14 — `proc.plan_*` needs an
   application/execution check before full credit) and symbolic-coefficient
   blindness (cyclist ½-factor — `eq.*` needs a non-NLI symbolic tier;
   REPORT.md §10.3).
3. **Corpus, not resolver** (G4b): control negatives must be re-authored from
   transcripts, not persona scripts — after repair only 2–3 true control
   negatives survive, and the LLM role-players systematically leak scripted-
   omitted beats when Apollo asks (5 of 7 flips follow a clarification
   question). Any future FCR gate needs a negative bank built by transcript
   audit (or personas hard-blocked from answering off-script). (REPORT.md
   §10.1.)
4. **G3-adjacent** misses by 2 attempts (25/31 vs 27/31 needed) — dominated by
   the same misconception attempts where V2-Strong vs LLM-Developing disagree
   (e.g. attempt 1); expected to move with blocker 1, re-measure after.

Artifacts: `corrected-negatives-2026-07-07.json` (all 22 decisions with
verbatim quotes + recomputed numbers, both treatments). `campaign/out/f1c/**`
untouched.
