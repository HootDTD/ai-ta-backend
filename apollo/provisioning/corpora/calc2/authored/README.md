# Calculus 2 Authored Problem+Solution Corpus
## Authorship
All 60 problems and their worked solutions in this corpus are ORIGINAL content, authored on 2026-07-07 for Hoot's authored-sets provisioning feature. They were written fresh in the general style of classic topical calculus problem sets. No problem wording, scenario, or solution was copied from Paul's Online Notes, OpenStax, Stewart, or any other existing source; only standard mathematical content (e.g. the integral of x*e^x) is shared, which is not copyrightable. There is no license encumbrance on this corpus; it may be used freely within Hoot.
## Corpus statistics
- Total problems: 60 (all with full step-by-step worked solutions ending in an ANSWER line, satisfying the paired-solution grounding contract).
- Concepts (6 problems each):
  - alternating-series: 6
  - comparison-tests: 6
  - improper-integrals: 6
  - integration-by-parts: 6
  - numerical-integration: 6
  - partial-fractions: 6
  - ratio-root-tests: 6
  - trigonometric-integrals: 6
  - trigonometric-substitution: 6
  - u-substitution: 6
- Difficulty spread: easy(1)=17, medium(2)=27, hard(3)=16

## Verification
Every answer was independently checked with SymPy (see verify.py):
- Indefinite integrals: d/dx(answer) simplified against the integrand.
- Definite/improper integrals: SymPy `integrate` compared to the stated value; divergence confirmed as non-finite.
- Numerical-integration problems: the trapezoid/midpoint/Simpson rule and the error-bound n were recomputed from scratch and matched.
- Series problems: SymPy `Sum(...).is_convergent()` on the series and its absolute-value series, plus the ratio/root limit where relevant.

**Result: 60 / 60 PASS.**

No problems required manual flagging; SymPy handled all 60.

## Homework set composition
6 mixed sets of 10 problems each (deterministic shuffle, seed=1). Each set mixes at least 5 distinct concepts in shuffled order to mimic realistic teacher assignments (NOT concept-segregated). concept_slug is ground truth and is kept private from the matcher.

- **HW1** (6 concepts): comparison-tests x2, integration-by-parts x1, partial-fractions x1, ratio-root-tests x1, trigonometric-substitution x3, u-substitution x2
- **HW2** (8 concepts): improper-integrals x1, integration-by-parts x2, numerical-integration x1, partial-fractions x2, ratio-root-tests x1, trigonometric-integrals x1, trigonometric-substitution x1, u-substitution x1
- **HW3** (7 concepts): alternating-series x3, comparison-tests x1, improper-integrals x1, numerical-integration x2, ratio-root-tests x1, trigonometric-integrals x1, trigonometric-substitution x1
- **HW4** (7 concepts): alternating-series x1, comparison-tests x2, improper-integrals x2, numerical-integration x1, trigonometric-integrals x2, trigonometric-substitution x1, u-substitution x1
- **HW5** (8 concepts): comparison-tests x1, improper-integrals x1, integration-by-parts x1, numerical-integration x1, partial-fractions x3, ratio-root-tests x1, trigonometric-integrals x1, u-substitution x1
- **HW6** (7 concepts): alternating-series x2, improper-integrals x1, integration-by-parts x2, numerical-integration x1, ratio-root-tests x2, trigonometric-integrals x1, u-substitution x1

## Discrimination pressure
Within each concept cluster, some problems are designed to superficially resemble a neighboring concept (recorded in the `looks_like` field, metadata only). Examples:
- usub-02 (u-substitution) looks like partial-fractions
- usub-03 (u-substitution) looks like integration-by-parts
- usub-04 (u-substitution) looks like trigonometric-integrals
- usub-05 (u-substitution) looks like integration-by-parts
- usub-06 (u-substitution) looks like trigonometric-substitution
- ibp-03 (integration-by-parts) looks like u-substitution
- ibp-06 (integration-by-parts) looks like integrals-inverse-trig
- trigint-04 (trigonometric-integrals) looks like u-substitution
- trigint-05 (trigonometric-integrals) looks like integration-by-parts
- trigsub-01 (trigonometric-substitution) looks like integrals-inverse-trig
- trigsub-06 (trigonometric-substitution) looks like u-substitution
- pf-03 (partial-fractions) looks like integrals-inverse-trig
- pf-05 (partial-fractions) looks like u-substitution
- improp-05 (improper-integrals) looks like integration-by-parts
- improp-06 (improper-integrals) looks like comparison-tests
- cmp-03 (comparison-tests) looks like ratio-root-tests
- cmp-04 (comparison-tests) looks like divergence-and-integral-tests
- cmp-05 (comparison-tests) looks like alternating-series
- cmp-06 (comparison-tests) looks like divergence-and-integral-tests
- rr-03 (ratio-root-tests) looks like comparison-tests
- rr-06 (ratio-root-tests) looks like comparison-tests

## Files
- `authored_corpus.json` - all 60 problems with set/position/metadata.
- `hw{1..6}_problem.pdf` / `hw{1..6}_solution.pdf` - the paired PDFs (same numbering).
- `problems.py` - source data. `verify.py` - the SymPy checker. `build.py` - this generator.
