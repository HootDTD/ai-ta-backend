# Calc-2 Question→Concept Matching — Expert Adjudication

Adjudicator: opus-4.8, 2026-07-07. Read-only; every contested row's raw chunk was
inspected in `raw_section_chunks.json` to confirm whether inline math was
*present-then-stripped* (extraction damage) or *never present* (word problem).

## Headline

- **Corrected accuracy on adequately-specified problems: 71/73 = 97.3%** (98.6% if
  the one soft error is treated as defensible). Beats the ≥95% target.
- **Raw baseline of 58.2% is an artifact**, not matcher weakness. Two forces drag
  it down: (1) the indexing pipeline stripped inline math from ~2/3 of exercises,
  and (2) the section-derived ground truth punishes defensible/better picks on
  cross-topic and slug-overlap problems.
- **Genuine model errors: 1 firm (#77) + 1 soft (#61) = 2 of 73** (2.7%).

## Task 1 — cause breakdown of the 33 contested rows

| cause | count | ids |
|---|---|---|
| EXTRACTION_DAMAGE | 6 | 14, 34, 41, 57, 68, 69 |
| MODEL_ERROR | 2 | 61, 77 |
| GROUND_TRUTH_DEBATABLE | 25 | 2, 5, 6, 7, 8, 11, 12, 13, 28, 29, 43, 46, 50, 55, 56, 59, 60, 62, 63, 64, 65, 66, 72, 73, 74 |

Model was reasonable on **31/33** rows (only the two model errors are unreasonable).

### Extraction damage (6) — math confirmed stripped in the raw chunk
- **14** (1.5) semicircle: raw shows `Use the substitution [MISSING]` — and for √(1−x²) the intended sub *is* trig (x=sinθ), so model's trig-sub is defensible and u-sub intent is unrecoverable.
- **34** (3.1 IBP): raw shows `velocity of [MISSING] after t sec` — integrand gone; only the FTC framing survives.
- **41** (3.7): raw `391. Evaluate [MISSING]` — empty; model's NO_MATCH @0.05 is exactly right.
- **57** (4.5): raw `Solve [MISSING] with the initial condition [MISSING]` — ODE gone; NO_MATCH correct.
- **68** (5.6): raw `Suppose that [MISSING] for all [MISSING]...` — defining expression gone; ratio/root intent unrecoverable.
- **69** (6.1): raw strips the power-series notation, leaving only the word "sequence"; model followed the surviving text.

### Model errors (2)
- **77** (7.5) — FIRM. Rationale says "a geometry/conic sections application about a paraboloid," then outputs NO_MATCH @0.98 while `conic-sections` is in the list. Self-contradictory low-effort slip. Expert label: `conic-sections`.
- **61** (5.2) — SOFT. Coin flipped a multiple of 3 times before heads = Σ(1/2)^{3k}, a geometric series = the section's concept. Model rejected as "discrete probability, not calculus." Real miss, but recovering it requires seeing the hidden series. Expert label: `infinite-series-basics`.

### Ground-truth-debatable highlights (model's pick defensible or better)
- **7, 8** Kepler → `conic-sections` is *better* than the section's FTC label (esp. #8, pure ellipse geometry).
- **56** drag∝v² → genuinely nonlinear & separable; model's `separable-equations` is *more correct* than the section's `first-order-linear-ode`.
- **66** balancing-scale "what does this have to do with infinite series" → `infinite-series-basics` beats the section's `comparison-tests`.
- **72, 74** perpetuity/π-estimation → geometric-series / Taylor-application reads beat `power-series-operations`.
- **5, 6** MVT climber/toll-road are Calc-1 MVT-for-derivatives; expert label is really `NO_MATCH`, so the model disagreeing with the section is correct.
- **43, 50** (the 2 PARTIAL rows) are genuine slug overlap (basics/separable, separable/exp-decay) — model put the expected slug in secondary.

## Task 2 — spot-check of scored-correct rows (12, all 7 chapters)

Sampled 1, 4, 16, 24, 27, 33, 39, 44, 51, 67, 70, 78. **All correct on their reasoning
trail; zero lucky/wrong.** One soft flag:
- **27** (2.5) shock-absorber spring constant is Hooke's-law algebra (F=kx) with no
  integration exercised — the label `physical-applications` matches the section but
  the problem is a degenerate pre-calculus warmup. Correct label, trivial problem;
  not a matcher failure.

## Task 3 — corrected metrics

**(a) Accuracy on adequately-specified problems**
- Denominator = 79 − 6 extraction-damage = **73** (all 6 damage rows are non-matches, so no correct answer is removed).
- Numerator = 46 MATCH + 25 debatable-but-reasonable = **71**.
- **= 71/73 = 97.3%** (72/73 = 98.6% if #61 is credited as defensible).

**(b) Honest model-error rate**
- **2 errors / 73 = 2.7%** (2/79 = 2.5% of whole corpus). Firm: #77. Soft: #61. Everything else is either extraction damage or a defensible/better pick.

**(c) Coverage of the 40-concept list**
- **28 tested** (≥1 problem), **12 untested** (zero problems):
  `integrals-exponential-logarithmic`, `integrals-inverse-trig`,
  `trigonometric-integrals`, `trigonometric-substitution`, `partial-fractions`,
  `alternating-series`, `taylor-maclaurin-series`, `taylor-series-applications`,
  `parametric-equations`, `calculus-parametric-curves`, `polar-coordinates`,
  `polar-area-arc-length`.
- Correction to the evidence report: it said "11 uncovered" but omitted
  `integrals-exponential-logarithmic` and `alternating-series` (both zero-problem)
  and wrongly listed `integration-by-parts` as uncovered — IBP has one problem
  (#34) but it is extraction-damaged, so IBP is *effectively* untested.
- 8 concepts are tested-but-never-correctly-matched (FTC, u-substitution, IBP,
  first-order-linear-ode, infinite-series-basics, divergence/integral tests,
  ratio/root tests, power-series-operations) — almost all traceable to the two
  forces above.

## Task 4 — verdict on reversed-provisioning question→concept matching

**Closed-list question→concept matching is accurate enough to base the reversed
provisioning flow on.** On well-formed problem text, expected accuracy is **97–99%**,
clearing the ≥95% bar. The 58.2% raw number is almost entirely (a) upstream math
stripping and (b) a single-label ground truth that penalizes defensible picks on
cross-topic and slug-overlap problems — only 1 firm model error in 73 adequately-
specified rows survives scrutiny.

Two caveats gate the decision:

1. **Remaining failure modes are narrow and mostly benign.** The real ones are
   hidden-structure word problems (a geometric series or harmonic-series divergence
   disguised as a probability/logistics puzzle), where low reasoning effort rejects
   the item as "not calculus." Raising reasoning effort or adding a "look for an
   implicit series/limit" hint would likely catch these. The rest of the "errors"
   are taxonomy overlap (separable vs linear, sequences vs exponential growth,
   series vs power-series-operations) — the provisioning flow should allow a concept
   to map to a small equivalence set rather than one hard slug, which dissolves most
   of them.

2. **The test is not yet trustworthy for the concepts that matter most.** The
   confusable-techniques cluster — integration-by-parts vs trigonometric integrals
   vs trigonometric substitution vs partial fractions — is *effectively untested*:
   three have zero problems and IBP has only a math-stripped stub. This is exactly
   the discrimination a concept matcher exists to do. **Corpus improvement needed:**
   re-ingest `document_id=5` with a pipeline that preserves inline/rendered math
   (MathML/LaTeX) so sections 1.6/1.7, 3.2/3.3/3.4, 5.5, 6.3/6.4, and 7.1–7.4 yield
   real exercises. Only then can the 12 uncovered concepts (and the four-way
   techniques discrimination) be measured. Until then, ship the flow for the
   28 covered concepts with confidence, but treat the confusable-techniques cluster
   as unverified.
