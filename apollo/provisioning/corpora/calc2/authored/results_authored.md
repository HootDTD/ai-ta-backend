# Blind Question -> Concept Matcher: Authored Calc 2 Corpus

Model: `gpt-5.1`, reasoning effort `low` for pass 1 over all 60 problems; effort `medium`
retried only on the problems pass 1 got wrong (or NO_MATCH). Prompt = problem_text only
plus the full 40-concept list (name/slug/desc). Model was BLIND to `concept_slug`,
`looks_like`, and `set` for every call. Concurrency 8, retry-once-on-parse-failure (0
parse errors this run).

Corpus: 60 problems, 10 concepts x 6 each, 21 "plants" (`looks_like` != null),
difficulty distribution {1: 17, 2: 27, 3: 16}.

## Accuracy table

| Slice | Pass1 (low) | Final (low, medium-retry on misses) |
|---|---|---|
| **Overall** | 57/60 = 0.950 | 58/60 = 0.967 |
| Plants (n=21) | 19/21 = 0.905 | 19/21 = 0.905 |
| Non-plants (n=39) | 38/39 = 0.974 | 39/39 = 1.000 |

### Per-concept (n=6 each)

| Concept | Pass1 | Final |
|---|---|---|
| alternating-series | 6/6 = 1.000 | 6/6 = 1.000 |
| comparison-tests | 6/6 = 1.000 | 6/6 = 1.000 |
| improper-integrals | 6/6 = 1.000 | 6/6 = 1.000 |
| integration-by-parts | 4/6 = 0.667 | 5/6 = 0.833 |
| numerical-integration | 6/6 = 1.000 | 6/6 = 1.000 |
| partial-fractions | 6/6 = 1.000 | 6/6 = 1.000 |
| ratio-root-tests | 6/6 = 1.000 | 6/6 = 1.000 |
| trigonometric-integrals | 6/6 = 1.000 | 6/6 = 1.000 |
| trigonometric-substitution | 5/6 = 0.833 | 5/6 = 0.833 |
| u-substitution | 6/6 = 1.000 | 6/6 = 1.000 |

All 3 misses land on exactly two concepts: `integration-by-parts` (2 misses,
both against `integrals-exponential-logarithmic`) and `trigonometric-substitution`
(1 miss, against `integrals-inverse-trig`).

### Per-difficulty

| Difficulty | Pass1 | Final |
|---|---|---|
| 1 (n=17) | 15/17 = 0.882 | 16/17 = 0.941 |
| 2 (n=27) | 26/27 = 0.963 | 26/27 = 0.963 |
| 3 (n=16) | 16/16 = 1.000 | 16/16 = 1.000 |

Counter-intuitively, difficulty-3 problems are matched *more* reliably than
difficulty-1 problems — the misses are all short, single-technique-lookalike
problems (see below), not hard multi-step ones.

### Secondary-credit rate

- Among the 3 pass-1 misses, truth appeared in the model's `secondary` list for **2/3 (0.667)**.
- Across all 60 problems, truth appeared in `secondary` for 2/60 (0.033) overall (the same 2 miss cases — no case where a *correct* primary also duplicated truth into secondary, as expected since secondary excludes primary).

### Confusion pairs (pass1, truth -> predicted)

| Truth | Predicted | Count |
|---|---|---|
| integration-by-parts | integrals-exponential-logarithmic | 2 |
| trigonometric-substitution | integrals-inverse-trig | 1 |

## Every miss

**1. `ibp-01`** — truth=`integration-by-parts`, not a plant, difficulty 1.
Problem: "Evaluate integral x e^x dx."
- Pass1 (low) predicted `integrals-exponential-logarithmic` (conf 0.96). Rationale: "The integral involves e^x multiplied by a polynomial, a standard exponential integral often approached with substitution or basic antiderivative rules." Secondary was `['u-substitution', 'antiderivatives-basic-integration']` — truth NOT captured in secondary for this one.
- Pass2 (medium) predicted `integration-by-parts` (conf 0.99) — **FIXED**. Rationale: "∫x e^x dx is a standard product requiring integration by parts, with the exponential function involved as a secondary feature."

**2. `ibp-03`** — truth=`integration-by-parts`, **plant** (`looks_like=u-substitution`), difficulty 2.
Problem: "Evaluate integral ln(x) dx."
- Pass1 (low) predicted `integrals-exponential-logarithmic` (conf 0.94). Rationale: "...an antiderivative involving a logarithmic function, typically solved using integration by parts, so it mainly fits exponential/logarithmic integrals with integration by parts as a supporting technique." Secondary correctly included `integration-by-parts` (truth captured in secondary).
- Pass2 (medium) predicted `antiderivatives-basic-integration` (conf 0.97) — **STILL WRONG** (different wrong answer). Rationale: "the problem asks for an indefinite integral (an antiderivative), and the standard method for ∫ln x dx uses integration by parts" — model's own rationale names the correct technique but still doesn't select it as primary.

**3. `trigsub-01`** — truth=`trigonometric-substitution`, **plant** (`looks_like=integrals-inverse-trig`), difficulty 1.
Problem: "Evaluate integral 1/sqrt(9 - x^2) dx."
- Pass1 (low) predicted `integrals-inverse-trig` (conf 0.97). Rationale: "matches the standard arcsin inverse trig form ∫dx/√(a²−x²)". Secondary correctly included `trigonometric-substitution` (truth captured in secondary).
- Pass2 (medium) predicted `integrals-inverse-trig` again (conf 0.98) — **STILL WRONG**, same rationale, unchanged by extra reasoning effort.
- Note: this is arguably a genuinely ambiguous ground-truth case — ∫dx/√(a²−x²) is the textbook arcsin lookup form (`integrals-inverse-trig`) *and* is standardly taught/solvable via the trig-sub x = a sin θ (`trigonometric-substitution`); the authored corpus's `looks_like` field itself flags this exact tension by design (it's a discrimination plant). The model's answer is defensible even though it doesn't match the authored label.

## Effort-related-misses hypothesis: verdict

Mixed support. 1 of 3 misses (`ibp-01`) was fixed purely by bumping effort
low -> medium with an identical prompt, supporting the adjudicator's hypothesis
that some misses are effort-limited rather than knowledge-limited. The other 2
(`ibp-03`, `trigsub-01`) were NOT fixed by extra effort — `ibp-03` flipped to a
different wrong answer, and `trigsub-01` reproduced the identical wrong answer
with near-identical rationale, indicating a genuine boundary-concept ambiguity
(ln x and 1/√(a²−x²) both have a legitimate "shortcut" concept plus the
intended-technique concept) rather than a reasoning-depth problem. Net:
medium-effort retry converted 1/3 misses -> overall final accuracy 58/60 (0.967)
vs pass1 57/60 (0.950).

## Plant sensitivity

Non-plant accuracy was already high (0.974 pass1, 1.000 final). Plant accuracy
was lower and did NOT improve with the medium-effort retry (0.905 both passes)
— both plant misses (`ibp-03`, `trigsub-01`) survived the extra-effort retry.
This is consistent with plants being harder in a *structural* way (a genuinely
competing concept is textually present), not an effort/attention way — extra
reasoning effort does not overturn a well-formed alternative reading of the
same problem text.
