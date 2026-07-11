# Misconception-Detector A/B Validation — 2026-07-08

**Mode:** `full_judge` (real OpenAI `make_openai_judge()`, gpt-4o, temperature=0.0,
logprobs=True — never stubbed). Live local Docker Postgres (`127.0.0.1:57322`) +
Neo4j (`bolt://127.0.0.1:57687`). n = 20 attempts, 0 harness errors.
**Not committed** (per instructions).

Harness: `campaign/validate_misconception_detector.py` (reuses the scout's
reconstruction recipe: `KGStore.read_graph`, `list_problems_for_concept` +
`to_kg_graph` keyed on `attempt.problem_id`, `Message role='student'` query;
`detect_misconceptions → gate_findings → merge_detections → apply_penalty`
called directly; banding via `apollo.projections.scorecard.load_bands/_band_for`).
Baseline composite read from `attempt.diagnostic_report.rubric.overall.score`
(fallback `scorecard.score_0_100`) — **no second live grading run**.

Baseline truth (`campaign/out/v2-qa-2026-07-08/REPORT.md`): misconception
detection 0/40; ~45% of misconception personas false-Strong on the delivered
LLM grade. Success criteria (spec §7): false-Strong on misconception attempts
drops **materially** *without any* false positive on the strong controls
(a penalized strong control is a **hard fail**).

---

## 1. Verdict — did misconception findings + grading improve?

**No. Zero improvement. The A/B delta is exactly nil.**

- **0/20 attempts produced any gate-surviving (docked) finding.** Penalty = 0.0,
  `ceiling_applied = False`, `misconceptions_found = []` on all 20 rows.
- `baseline_band == detector_band` on **every single row** — no band moved by a
  single point in either direction.
- **Misconception detection: 0/16** misconception-class attempts flagged
  (ground truth expected ≥1 code on 13/16 of them). This is unchanged from the
  0/40 baseline — the detector, wired end-to-end, detected nothing.
- **False-Strong on misconception attempts: 7 → 7 (no change).** The gating
  success criterion ("drops materially") is **not met**. It did not drop at all.

This is a **clean negative result**, not a regression: the detector changed
nothing rather than making anything worse. But it delivered none of its intended
value on this dataset.

## 2. Which tier did the work? — None. All three were inert.

| Tier | Fired? | Why |
|---|---|---|
| **judge** (Tier-2 LLM) | **0 findings** | **Master defect (below).** Every one of 20 attempts logged `misconception judge returned malformed JSON, soft-failing` and decayed to `verdict=clear, confidence=0.0`. |
| **bank_pattern** | 0 findings | Contributed nothing on this dataset; no exceptions logged. Not deep-dived (see caveats). |
| **sympy_veto** | 0 findings | Contributed nothing; no exceptions logged. Named tier is likely **inert by construction** on this data (no sign-mutant graph pairs seeded). |

The judge is the **only tier capable of producing findings here**, and it
soft-failed on 100% of attempts. With nothing from the judge, `gate_findings`
had nothing to dock and nothing to corroborate — so bank_pattern/sympy_veto (had
they fired) would also have lacked corroboration. **The whole detector was
effectively a no-op on this run.**

### Master defect (confirmed, reproducible, upstream — not a harness artifact)

`apollo/overseer/misconception_detector/judge.py :: make_openai_judge()` requests
one row per concept inside a **top-level `{"concepts": [...]}` array**. Live
gpt-4o (temperature=0.0, deterministic) consistently collapses the reply to a
**single flat object**, e.g.:

```json
{"concept_key": "net_exports", "verdict": "needs_clarification",
 "evidence_span": "", "confidence": 0.5}
```

`judge_concepts()` requires `parsed.get("concepts")` to be a `list`
(`judge.py:193-195`); the flat shape fails that check every time and soft-fails
to all-`clear`/confidence-0.0 per the module's documented contract
(`judge.py:199-201`, `_all_clear` at `:93`). Because confidence decays to 0.0 on
every concept, the gate has nothing above threshold to dock.

**Isolation:** reproduced **outside** the full `detect_misconceptions` pipeline
with minimal standalone 2-concept and 3-concept `make_openai_judge()` calls —
same single-object collapse both times, at temperature=0.0. This is a real
forced-JSON-schema / `response_format` contract defect in `judge.py`, **not** a
reconstruction or harness bug. (`response_format={"type":"json_object"}` alone
does not pin the multi-row array shape; the model prefers the flat object.)

Confirmed **not** stubbed: log line `mode=full_judge used_real_judge=True`, plus
the per-attempt `soft-failing` warning fires only on a genuinely
parsed-but-wrong-shaped LLM response (a stub returns literal `"{}"`, a
distinguishable code path).

## 3. Strong-control false-positive check (the gating constraint)

**0/4 control leaks — but VACUOUSLY, and this must not be read as a pass.**

Controls: 77, 89, 106 (strong), 97 (partial). Also 108
(`vague_then_clarifies`, misc2) was treated as **non-control** by the harness
because its `expected_class` is neither `strong` nor `partial` — noted for
completeness.

Every control stayed clean **only because the detector fired on nobody at all**,
not because it correctly discriminated controls from misconception attempts. The
hard-fail constraint (no penalized strong control) is **technically satisfied but
provides zero evidence of specificity** — you cannot false-positive with a
detector that never fires. The specificity question is **untested** until the
judge defect is fixed and the detector actually produces findings.

## 4. Honest caveats

- **Deterministic-only / single dataset.** All findings are from one
  temperature=0.0 run over 20 reconstructed v2-qa attempts. No sampling,
  variance, or multi-seed check.
- **What was validated:** the harness wiring
  (`detect_misconceptions → gate_findings → merge_detections → apply_penalty`)
  runs end-to-end with the real judge, 0 errors; and the judge-schema-collapse
  defect is real, deterministic, and reproducible in isolation.
- **What was NOT validated:** whether the detector *improves grading or catches
  misconceptions* — it caught none, so the core hypothesis is **unproven, not
  refuted**. Also unvalidated: control-specificity (see §3, vacuous), and the
  behavior of any tier on a passing judge.
- **Small n / class imbalance.** 16 misconception vs 4 controls; 13/16
  misconception rows carry ground-truth codes. Too few controls to establish a
  specificity rate even if the detector had fired.
- **Reconstruction limits.** Baseline composite was read from the recorded
  `diagnostic_report`/scorecard, not re-graded live; graphs/queries were
  rebuilt from `problem_id` + `Message role='student'`. If reconstruction under-
  or mis-populated any student graph, that could independently suppress
  bank_pattern/sympy_veto — this is a confound the run did not control for.
- **sympy_veto likely inert by construction.** No sign-mutant reference/student
  graph pairs were seeded, so the named sympy_veto tier may have had no
  triggering condition on any pair regardless of the judge. Its 0-contribution
  is **not** evidence it works or fails — it was untested.
- **bank_pattern 0-contribution not root-caused.** Did not confirm whether the
  `apollo_misconceptions` bank rows cosine-match any utterance above
  `BANK_SIM_FLOOR=0.80`. Separate second-order question, deferred.
- **Cosmetic:** a NumPy 2.x / pandas ABI warning from the neo4j driver's
  optional pandas import appears in stderr — harmless, import succeeds,
  unrelated, not fixed.

## 5. Recommended next step (blocking)

Fix the judge JSON-schema contract in `judge.py` so gpt-4o emits the
`{"concepts": [...]}` array reliably — options in rough order of robustness:

1. Use a **strict `json_schema` response_format** (structured outputs) pinning a
   top-level `concepts` array with `minItems`, rather than a free
   `json_object` + prose instruction.
2. If staying on `json_object`: add a **flat-object fallback parse** in
   `judge_concepts()` — when `concepts` is absent but the object looks like a
   single concept row, wrap it as a 1-element list keyed by its `concept_key`
   (only a partial fix: it recovers 1-concept batches, not multi-concept ones).
3. Send **one concept per call** (N calls) so the single-object reply is the
   correct shape by construction — simplest, costs N× latency/tokens.

Then **re-run this exact A/B** and re-evaluate §1 (detection rate, false-Strong
drop) and §3 (control specificity, now non-vacuous). Only after the judge fires
can bank_pattern/sympy_veto's real contributions and the gate's
corroboration/specificity be assessed. Sign-mutant seeding is a prerequisite for
any claim about sympy_veto.

---

## 6. Re-run after judge fix — 2026-07-08 (honest verdict)

The structured-outputs JSON-schema fix from §5 option 1 is **live and
independently verified working**. The harness re-ran clean end-to-end
(`mode=full_judge`, `used_real_judge=True`, 20/20 attempts, 0 errors; the only
stderr noise was the same harmless NumPy/pandas ABI warning at neo4j-driver
import, non-fatal). **No code changes were made in this re-run and nothing was
committed** — this section reports the observed behavior with the already-shipped
judge fix in place.

### 6.1 Did the judge fix actually work? — YES (verified)

A direct probe bypassing the corroboration gate — dumping
`detect_misconceptions().per_concept` **pre-gate** on attempts 75/88/95/110/77 —
confirms the judge now returns **real, varied per-concept verdicts and
confidences** (e.g. 0.70, 0.952, 0.973, 0.636, 0.996; mixes of `clear` and
`needs_clarification`), **not** the pre-fix all-`clear`/confidence-0.0 collapse.
The `{"concepts": [...]}` array shape is now emitted reliably. **The master defect
from §2 is fixed and confirmed.**

### 6.2 Did misconception findings + grading improve? — NO. Headline numbers did not move.

**The A/B delta is still exactly nil, but for a *different, now-understood*
reason** (the gate's corroboration contract, not judge JSON parsing):

- **Misconception detection: 0/16** misconception-class attempts produced ≥1
  `misconceptions_found` — **unchanged in raw count** from the pre-fix run.
- **False-Strong on misconception attempts: 7 → 7 (no reduction).** The gating
  success criterion ("drops materially") is **still not met.**
- **`baseline_band == detector_band` on all 20 rows**; penalty = 0.0 everywhere;
  `misconceptions_found = []` everywhere. (Full 20-row table below.)

This is **not a residual judge defect.** It is the gate's *documented
corroboration contract*: `gate_findings()`
(`apollo/overseer/misconception_detector/gate.py`) only docks a finding to
`verdict="misconception"` when a **deterministic `sympy_veto` is present OR ≥2
independent sources agree** with a judge finding clearing tau. A **lone judge
signal always downgrades to `verdict="needs_clarification"` / `corroborated=False`
regardless of confidence**, and `merge_detections` only promotes
`corroborated=True + verdict="misconception"` rows into `outcome.misconceptions`.
In this sample `sympy_veto` and `bank_pattern` **both fired zero findings**, so
the now-working judge findings had no corroboration and could never surface.

### 6.3 Tier attribution (this re-run)

100% of observed per-concept findings across the probed sample (75/88/95/110/77)
came from **`source="judge"` only**. `sympy_veto`: **0 findings.** `bank_pattern`:
**0 findings.** Every judge finding's signature was **`unkeyed:<concept_id>`**
(never a bank-keyed `misc.<code>`), consistent with `bank_pattern` not having
fired to seed a keyed signature. The judge is architecturally a **single source**,
so it is structurally incapable of self-corroborating — hence uncorroborated,
hence no dock.

### 6.4 STRONG-CONTROL FALSE POSITIVE (the hard constraint) — NONE. ✅

**0 strong-control false positives. The hard-fail constraint is satisfied — and
this time it is NOT vacuous in the trivial "detector never fires" sense: the judge
now genuinely fires on controls and still correctly clears them.**

- Strong controls **77, 89, 106** and partial control **97**: all four show
  `penalty=0.0`, `misconceptions_found=[]`, `baseline_band==detector_band`,
  `control_credit_ok=True`.
- Attempt **77**'s judge verdicts were correctly `clear` across all 5 concepts at
  ~0.996 confidence — i.e. the judge fix did **not** introduce over-firing on
  controls. **No new calibration problem on controls was introduced.**

**Plainly stated: the judge does NOT over-fire on controls. There is no new
false-positive calibration regression.** (Note the *reason* controls stayed clean
is still partly the same corroboration gate that suppresses everything — but
unlike the pre-fix run, attempt 77 demonstrates the judge itself independently
returned `clear` at high confidence, so control specificity now has at least one
real data point behind it, not zero.)

### 6.5 Full 20-row result table

| id | expected_class | baseline_band(score) | detector_band(score) | penalty | misc_found | raw/gated | ctrl_ok | notes |
|----|----------------|----------------------|----------------------|---------|-----------|-----------|---------|-------|
| 75  | misconception          | Developing(0.67) | Developing(0.67) | 0.0 | [] | 5/5 | True | 5 gated rows all judge-only `needs_clarification` (unkeyed:*); no dock |
| 77  | strong (CONTROL)       | Strong(0.96)     | Strong(0.96)     | 0.0 | [] | 5/5 | True | all 5 judge verdicts `clear`, conf~0.996; correctly zero-penalty |
| 81  | misconception          | Strong(0.95)     | Strong(0.95)     | 0.0 | [] | 5/0 | True | 5 raw judge findings, 0 survive gate (no corroboration) |
| 88  | misconception          | Strong(0.90)     | Strong(0.90)     | 0.0 | [] | 4/4 | True | judge real (conf 0.952, mixed) but all unkeyed + uncorroborated → no dock |
| 89  | strong (CONTROL)       | Strong(0.90)     | Strong(0.90)     | 0.0 | [] | 4/4 | True | gated rows `needs_clarification`, not misconception; ctrl clean |
| 95  | misconception          | Strong(0.90)     | Strong(0.90)     | 0.0 | [] | 4/4 | True | judge conf 0.973, same unkeyed/uncorroborated pattern |
| 97  | partial (CONTROL)      | Developing(0.50) | Developing(0.50) | 0.0 | [] | 5/0 | True | control clean |
| 100 | misconception          | Strong(0.90)     | Strong(0.90)     | 0.0 | [] | 4/4 | True | no dock |
| 102 | misconception          | Developing(0.65) | Developing(0.65) | 0.0 | [] | 7/7 | True | no dock |
| 105 | misconception          | Strong(0.95)     | Strong(0.95)     | 0.0 | [] | 4/4 | True | no dock |
| 106 | strong (CONTROL)       | Strong(0.93)     | Strong(0.93)     | 0.0 | [] | 5/0 | True | control clean |
| 108 | vague_then_clarifies   | Developing(0.67) | Developing(0.67) | 0.0 | [] | 5/0 | True | no dock |
| 109 | misconception          | Developing(0.64) | Developing(0.64) | 0.0 | [] | 5/0 | True | no dock |
| 110 | misconception          | Strong(1.00)     | Strong(1.00)     | 0.0 | [] | 5/5 | True | judge conf 0.635, 2 `clear` (final_goods_only/compute_net_exports); still no dock |
| 111 | misconception          | Proficient(0.71) | Proficient(0.71) | 0.0 | [] | 5/5 | True | no dock |
| 112 | misconception          | Strong(0.90)     | Strong(0.90)     | 0.0 | [] | 4/4 | True | no dock |
| 113 | misconception          | Developing(0.67) | Developing(0.67) | 0.0 | [] | 5/0 | True | no dock |
| 114 | misconception          | Beginning(0.49)  | Beginning(0.49)  | 0.0 | [] | 3/0 | True | no dock |
| 115 | misconception          | Beginning(0.42)  | Beginning(0.42)  | 0.0 | [] | 3/0 | True | no dock |
| 116 | misconception          | Proficient(0.79) | Proficient(0.79) | 0.0 | [] | 3/0 | True | no dock |

### 6.6 What still needs work (the real bottleneck is now the gate + bank, not the judge)

1. **`bank_pattern` structural silence (root-caused this run).** The live stack
   sets `SUPABASE_DB_URL=postgresql+asyncpg://...`, so `_dialect_name` →
   `"postgresql"` → `detect_bank_pattern` takes the `_postgres_match` branch →
   `misconception_bank.match_by_embedding`, which cosine-matches the **utterance
   embedding against `description_embedding` ONLY** (never `trigger_phrases`;
   those columns are dead weight on this path). Measured cosine (production
   `embed_texts` path, `text-embedding-3-large`, correctly scoped to each
   attempt's `concept_id`):

   | Attempt | Concept | Best-vs-description sim | Code matched | Clears 0.80? |
   |---|---|---|---|---|
   | 88  | 3  | **0.582** | nominal_for_real   | No |
   | 95  | 10 | **0.614** | nominal_for_real   | No |
   | 110 | 11 | **0.675** | includes_transfers | No |

   All three pick the **correct** top code (scoping/retrieval work) but fall
   0.13–0.22 **below** the 0.80 floor. This is a **threshold-vs-representation
   mismatch, not a scoping bug**: (a) utterance-vs-*trigger-phrase* text scores
   ~0.06–0.13 higher (0.645–0.707) than utterance-vs-description, but the code
   never embeds trigger phrases; (b) even best-case tops out ~0.65–0.71, still
   under 0.80 — and unrelated codes on the same concept
   (`deflate_wrong_direction` vs `nominal_for_real`) sit at 0.74 cross-similarity,
   so 0.80 guarantees zero recall while any workable floor risks conflating
   sibling codes. Concrete fixes (not applied): **index `trigger_phrase`
   embeddings in addition to `description`** (closest to an unambiguous bug), and
   **calibrate the floor off a labeled ROC/PR pass over the full 20-set** rather
   than the current generic 0.80. Measurement script:
   `scratchpad/measure_bank_sim.py` (live Postgres, production embed path, no
   doubles).

2. **`sympy_veto` unseeded / inert by construction.** No sign-mutant
   reference/student graph pairs were seeded, so the deterministic veto tier had
   no triggering condition on any pair. Its 0-contribution is **untested, not
   evidence of health.** Seeding sign-mutant pairs is a prerequisite for any
   `sympy_veto` claim — and, given the corroboration gate, is currently the
   **only** path that could dock a misconception on this dataset without a second
   independent tier.

3. **Gate corroboration calibration (the true headline bottleneck).** With a
   working judge, the binding constraint is that **a lone judge finding never
   docks.** On this dataset the judge is the *only* tier producing findings, so
   nothing ever corroborates and detection stays 0/16. Either (a) seed
   `sympy_veto` and fix `bank_pattern` so a second independent tier can
   corroborate the judge, and/or (b) revisit whether a high-confidence,
   bank-**keyed** judge finding should be allowed to dock on its own — a
   **deliberate calibration decision**, not a bug to silently patch. Judge
   signatures being all `unkeyed:*` (never `misc.<code>`) is itself downstream of
   `bank_pattern` not firing to seed a keyed signature, so fixing bank matching
   also improves judge signature keying and thus corroboration eligibility.

### 6.7 Net

The judge JSON-schema defect is **fixed and independently verified**. The
headline detection numbers (**0/16 misconception detection, 7→7 false-Strong**)
**did not move**, because the actual bottleneck for this dataset was never the
judge parsing — it is the **corroboration-gate design (needs 2 independent tiers
or a deterministic veto) combined with `bank_pattern`'s threshold miss and
`sympy_veto` being unseeded**. The **hard constraint holds: zero strong-control
false positives, and this time non-vacuously** (attempt 77 shows the judge
independently clears controls at ~0.996). **No over-firing on controls was
introduced.** Next work is bank-matching representation/threshold + sympy-mutant
seeding + a gate-corroboration calibration decision — not more judge work.
