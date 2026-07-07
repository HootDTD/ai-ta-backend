# Resolver V2 — FINAL gate verdict: eqgate replay × corrected ledgers (2026-07-07)

- **Supersedes** the gate tables in `REPORT.md` (old ON replay × script-derived
  gold) and `REPORT-corrected-ledgers.md` (old ON replay × corrected gold).
  This is the composition of tonight's two fixes: the quote-audited ledger
  corrections (`corrected-negatives-2026-07-07.json`, commit 471471d) applied
  to the **equation text-credit-cap replay**
  (`replay-on-eqgate-2026-07-07.json`, commit 4d9be13). Pure recompute — zero
  code changes, `campaign/out/f1c/**` untouched, the dirty
  `apollo/subjects/**/problems/problem_02-05.json` working-tree diffs were
  READ-only inputs (same enumeration the calibrate script used).
- **Inputs:** OFF = `replay-off-2026-07-07.json`; ON =
  `replay-on-eqgate-2026-07-07.json`; LLM bands + personas =
  `campaign/out/f1c/attempts.jsonl` (`scorecard.band`); per-node corrected
  labels = `corrected-negatives-2026-07-07.json` (treatment A =
  borderlines-as-negative, B = borderlines-as-positive); original expected
  bands + per-node ON credits = `report-metrics-2026-07-07.json` +
  `traces-on-2026-07-07/`. Bands: Strong ≥ 0.85 / Proficient ≥ 0.70 /
  Developing ≥ 0.50 / else Beginning (reproduces all 62 report-metrics
  OFF/ON bands).
- **Method validation (old-replay reproduction):** the full pipeline here,
  run against the OLD ON replay, reproduces `REPORT-corrected-ledgers.md`
  **exactly** on every number: G1 0.0941, G2 1.000, G3 15/31 & 25/31, G4a
  {1, 34, 35}, G4b 1/3 (A) & 0/2 (B), all-negatives FCR 5/12 & 1/8, positive
  recall 120/124 & 124/128, corrected-expected agreement V2 19/31 & 26/31 (A)
  / 21/31 & 28/31 (B), LLM 17/31 & 24/31 (A) / 18/31 & 25/31 (B). The eqgate
  numbers below are computed by the identical code with only the replay file
  swapped.
- **Per-node "fired" under eqgate (reconstruction note):** the eqgate replay
  JSON has no per-node credits, so fired-status (credit ≥ 0.7, the
  pre-registered detection bar) was reconstructed deterministically from the
  old ON traces + the cap semantics: equation-type nodes on text paths capped
  to 0.3 (0.6 with ENTAIL pull-up — both < 0.7), everything else unchanged,
  except equation nodes the live run's v1 (SymPy) resolution restored to 1.0
  via the v1 floor. The restoration set was solved per attempt from the eqgate
  replay's observed `node_coverage`: **all 31 attempts solvable (grayzone-OFF
  confirmed), 30 unique**; attempt 37 admits two symmetric solutions
  (exactly ONE of eq.gdp_expenditure / eq.net_exports restored — the detected
  COUNT is determinate, only the identity is not, so no aggregate number is
  affected). Old-trace coverage also reproduces the old replay 31/31, and
  score-based fired matches the corrected-ledger `fired_replay` flags 19/19.

## 1. FINAL pre-registered gate table (eqgate ON × corrected ledgers)

Gates 1–3 do not read per-node negative labels (A = B for them). Gate 4 is
computed under both borderline treatments.

| # | Gate | Bar | OFF | ON eqgate | Corrected A | Corrected B | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | DISCRIMINATION: strong-mean − misconception-mean composite | ≥ 0.15 | 0.025 | **0.1192** | 0.1192 | 0.1192 | **FAIL** (improved from 0.0941; still −0.031 short — penalty subsystem, §6.1) |
| 2 | RECALL: median strong node_coverage | ≥ 0.60 | 0.6333 | **0.9625** | 0.9625 | 0.9625 | **PASS** |
| 3 | AGREEMENT vs frozen LLM band: exact / adjacent | ≥ 40% / ≥ 85% | 12.9% / 41.9% | **17/31 = 54.8% / 27/31 = 87.1%** | same | same | **PASS — first time both bars clear** (was 48.4% / 80.6%) |
| 4a | SAFETY: zero misconception controls banded Strong | 0 | 0 | **2** (attempts 34, 35) | 2 — both **misconception-PENALTY failures**, 0 coverage false-credits (§4) | same | **FAIL as written**; attempt 1 cleared by the eqgate; failure locus stays outside V2 coverage |
| 4b | SAFETY: replay control-negative FCR | ≤ 5% | — (no per-node OFF scores) | n/a (needs corrected labels) | **1/3 = 33.3%** → FAIL | **0/2 = 0%** → PASS | **UNDETERMINED** — 2–3 true negatives left; the corpus cannot decide a 5% bar (§6.3) |
| — | (info) replay all-negatives FCR | — | — | — | 5/12 = 41.7% | 1/8 = 12.5% | unchanged from corrected report — every surviving negative is a non-equation node, untouched by the cap |
| — | (info) replay positive recall (credit ≥ 0.7) | — | — | — | **104/124 = 83.9%** | **108/128 = 84.4%** | was 96.8% / 96.9% — the deliberate cost of the cap (§2) |
| — | (info) V2 vs corrected expected bands (exact / adjacent) | — | — | — | V2 **14/31 / 25/31** vs LLM 17/31 / 24/31 | V2 **16/31 / 27/31** vs LLM 18/31 / 25/31 | V2 **no longer beats the LLM on exact** (was 19 vs 17 / 21 vs 18); still ahead on adjacent — caveats in §5 |

## 2. Headline deltas (old ON → eqgate ON, same corrected ledgers)

- **G3 passes both bars for the first time** — exact 48.4% → 54.8%, adjacent
  80.6% → 87.1%. The 2-attempt adjacent gap that failed both prior reports is
  closed: the cap pulled V2's inflated Strongs down toward the LLM's bands.
- **G1 discrimination +0.025** (0.0941 → 0.1192, bar 0.15). The cap hits
  misconception personas (−0.0575 class mean) harder than strong personas
  (−0.0324) because strong personas' equations are more often SymPy-resolved
  (v1-floor-restored). Still short — the rest lives in the penalty term.
- **G4a violations 3 → 2**: attempt 1 (misconception, 0.879 Strong) drops to
  0.651 Developing — its eq.bernoulli/eq.continuity credit was text-only.
  Attempts 34/35 stay Strong at full coverage: their equations were
  symbolically executed (v1-restored), so the eqgate correctly does NOT
  touch them; only a working misconception penalty can (§4).
- **Measured positive recall −13 pts** (A: 120/124 → 104/124). 16 equation-
  node detections are withheld: taught-in-text-only equations now cap at
  0.3/0.6 < 0.7. Per the corrected ledgers these were genuinely taught in
  prose — the cap trades measured recall for immunity to the
  symbolic-coefficient false-credit class (the cyclist ½-factor mechanism,
  REPORT.md §10.3, now structurally closed: every full-credit equation node
  in the eqgate replay is v1/SymPy-verified). G2 stays PASS at 0.9625.
- **Zero movement on FCR rows** — all surviving corrected negatives are
  proc./cond./def. nodes; the cap only touches equation nodes.
- 13 attempts changed composite (12 down, −0.017…−0.228; 1 UP — the attempt-48
  flap, §3); **6 attempts changed band**.

## 3. Band changes old-ON → eqgate-ON (info row 6)

| attempt | persona | old ON | eqgate ON | Δ |
|---|---|---|---|---|
| 1 | misconception | 0.879 Strong | 0.651 **Developing** | −0.228 |
| 7 | partial | 0.854 Strong | 0.694 **Developing** | −0.159 |
| 15 | strong | 0.899 Strong | 0.786 **Proficient** | −0.113 |
| 18 | strong | 0.926 Strong | 0.836 **Proficient** | −0.090 |
| 27 | vague_then_clarifies | 0.895 Strong | 0.782 **Proficient** | −0.113 |
| 45 | strong | 0.882 Strong | 0.762 **Proficient** | −0.121 |

Composite-only movers (band unchanged): 3, 29, 36, 37, 40, 50 (all down) and
**48 (+0.030 UP — the nondeterminism flap)**. The eqgate is a pure credit cap
and cannot raise a composite; attempt 48's rise is run-to-run variance in the
LIVE grading path between the two replays: its `misc.gross_for_net`
misconception finding was detected in the old ON run and **absent in the
eqgate run** (same transcript; `unresolved_rate` also drifted 0.67 → 0.86).
The misconception-findings subsystem is not just too weak (§6.1) — it is
nondeterministic across identical replays.

## 4. Gate 4a — the 2 remaining misconception-Strong attempts, classified

Per the corrected ledgers both have **legitimately full node coverage** (the
gold flips: attempt 34 def.depreciation — defined AND executed 562−40=522;
attempt 35 eq.gdp_deflator — equation taught correctly in turns 0–2). Under
the eqgate their equation nodes are v1(SymPy)-restored to 1.0, so coverage
credit remains CORRECT: **zero V2 coverage false-credits; both are
misconception-penalty failures** — each persona also asserted its scripted
misconception (34: "GNP essentially equals NNP … without … depreciation";
35: multiply-instead-of-divide, answer wrong by ~10⁴×) and the composite's
penalty term (0.15·|misc|/|path| ≈ 0.03–0.05) cannot strip a band. Attempt 1
(the third violation in both prior reports) is cleared by the eqgate, not by
a penalty: its coverage was text-only equation credit.

## 5. Info — V2 vs LLM on corrected expected bands, per-class distributions

V2-eqgate drops below the frozen LLM on **exact** agreement vs the corrected
expected bands (A: 14/31 vs 17/31; B: 16/31 vs 18/31) while staying ahead on
**adjacent** (A: 25/31 vs 24/31; B: 27/31 vs 25/31). Two caveats make this
row weak evidence in either direction: (i) the corrected expected-band
formula still cannot represent "taught a serious misconception" — it bands
all three misconception controls Strong (REPORT-corrected-ledgers §4), which
V2 is now *penalized* for refusing on attempt 1; (ii) the ledger formula
counts a taught-in-prose equation as fully credited — exactly the credit the
eqgate deliberately withholds pending symbolic verification, so a drop here
is by design, not a regression. Non-adjacent (≥2-band) disagreements vs the
LLM: attempts 14, 16, 21 (V2 higher), 36 (V2 lower — LLM banded the scripted
nominal-for-real misconception persona Strong).

Per-class band distributions (OFF → ON-eqgate, 31 attempts):

| class | n | OFF | ON eqgate |
|---|---|---|---|
| strong | 10 | 5 Beg / 5 Dev | 6 Str / 4 Pro |
| partial | 8 | 6 Beg / 1 Dev / 1 Pro | 2 Str / 2 Pro / 3 Dev / 1 Beg |
| vague_then_clarifies | 6 | 4 Beg / 1 Dev / 1 Pro | 3 Str / 2 Pro / 1 Dev |
| misconception | 7 | 5 Beg / 2 Dev | 2 Str / 2 Pro / 3 Dev |

Class composite means OFF → eqgate: strong 0.419 → 0.860, partial
0.345 → 0.724, vague 0.459 → 0.798, misconception 0.394 → 0.741.

## 6. Remaining blockers (enumerated)

1. **Misconception penalty subsystem** (fails G1 and G4a). Findings are still
   v1's; the penalty contributes ≈0.03–0.05 of composite, so full-coverage
   misconception personas (34, 35) band Strong and the strong-vs-misconception
   gap stalls at 0.119. Now additionally shown **nondeterministic**: the
   attempt-48 flap (§3) — the same transcript's misconception finding
   appears/disappears across replays, moving the composite ±0.03. Both the
   magnitude and the stability of the penalty need rework before G1/G4a can
   pass on merit.
2. **proc.plan_\* bare-definition credit — NOT fixed.** The eqgate is
   equation-only by design; the plan-credit-from-bare-definition mechanism
   survives intact. Known instance: attempt 14
   `proc.plan_apply_flow_rate_definition` (nli 0.7 from a single "Q = A·v"
   definitional turn, never applied) — the one unambiguous replay false
   credit under treatment B (1/8). `proc.plan_*` nodes need an
   application/execution check before ≥0.7 credit, analogous to the equation
   cap (REPORT.md §10.3 mechanism #1).
3. **Control-negative bank re-authoring.** After ledger repair only 2–3 true
   control negatives survive; G4b cannot decide a ≤5% bar on that
   denominator under either treatment (1/3 FAIL vs 0/2 PASS). The LLM
   role-players systematically teach scripted-omitted beats when Apollo asks
   (5 of 7 flips followed a clarification question). A future FCR gate needs
   a negative bank authored from transcript audit — or personas hard-blocked
   from answering off-script.
4. **Human pass over the 22 corrected ledger rows.** The corrections are
   quote-grounded but LLM-adjudicated; the borderline class bounds the
   judgment calls, and every gate above is reported under both treatments —
   but any promotion decision leaning on the corrected FCR must be preceded
   by the (cheap) human audit of `corrected-negatives-2026-07-07.json`.

## 7. Promotion recommendation

**Do not promote resolver v2 on this evidence.** The pre-registered rules
require the safety gate to pass on trustworthy measurement, and neither half
of it does: G4a still shows 2 misconception controls banded Strong (correct
coverage, missing penalty — a real product harm: a student who teaches a
badly wrong idea with full beat coverage would be told "Strong"), and G4b is
undecidable because the repaired corpus retains only 2–3 true control
negatives (33.3% under treatment A vs 0% under B is noise, not a
measurement). G1 also remains below its bar (0.119 vs 0.15). What tonight's
two fixes DID establish should not be re-litigated: the 80% FCR headline was
a gold-label artifact; the symbolic-coefficient false-credit class is
structurally closed (every full-credit equation node is now SymPy-verified);
G2 and — for the first time — both G3 bars pass, with V2 at zero LLM grading
cost. The promotion path is now concrete and short: (1) rework the
misconception penalty (magnitude + the attempt-48 nondeterminism) so G4a/G1
can pass on merit, (2) add the plan-node application check (attempt 14), (3)
re-author the control-negative bank from transcript audits and have a human
confirm the 22 corrected rows — then re-run this exact gate table.

Artifacts: this file (final verdict); method + per-row quotes in
`REPORT-corrected-ledgers.md` / `corrected-negatives-2026-07-07.json`;
replays as named above. `campaign/out/f1c/**` untouched.
