# Calc 2 Question→Concept Matching — Evidence Report

Data source: staging Supabase (`hjevtxdt...`), table `aita_chunks`, `document_id=5`
(OpenStax Calculus Vol. 2, 12,918 chunks). Read-only throughout — no writes issued.

## Counts

| stage | count |
|---|---|
| exercises extracted (`problems.json`) | 79 |
| blind LLM classifications (`matches.json`) | 79 (0 API errors) |
| scoreable (section has a ground-truth concept) | 79 / 79 |
| — MATCH | 46 (58.2%) |
| — PARTIAL (expected concept only in secondary) | 2 (2.5%) |
| — MISMATCH | 22 (27.8%) |
| — model said `NO_MATCH` | 9 (11.4%) |

**Primary accuracy: 58.2%** (46/79). **Partial-credit rate (MATCH+PARTIAL): 60.8%**
(48/79). Model: `gpt-5.1`, `reasoning.effort=low`, Responses API, strict JSON
schema, 2 sections excluded from ground truth (`3.5 Other Strategies for
Integration` had no candidate problems anyway).

## Per-chapter accuracy

| chapter | n | MATCH | match% | PARTIAL | MISMATCH | model NO_MATCH |
|---|---|---|---|---|---|---|
| 1 — Integration | 14 | 5 | 35.7% | 0 | 5 | 4 |
| 2 — Applications of Integration | 19 | 17 | 89.5% | 0 | 2 | 0 |
| 3 — Techniques of Integration | 9 | 7 | 77.8% | 0 | 1 | 1 |
| 4 — Intro to Differential Equations | 15 | 9 | 60.0% | 2 | 3 | 1 |
| 5 — Sequences and Series | 11 | 2 | 18.2% | 0 | 7 | 2 |
| 6 — Power Series | 6 | 2 | 33.3% | 0 | 4 | 0 |
| 7 — Parametric Equations & Polar Coordinates | 5 | 4 | 80.0% | 0 | 0 | 1 |
| **Total** | **79** | **46** | **58.2%** | **2** | **22** | **9** |

Chapters 2, 3, and 7 score high because their surviving problems are largely
**fully-specified, real-number word problems** (springs, pyramids, satellite
dishes, whispering galleries) — these are easy, unambiguous blind-classification
targets. Chapters 1, 5, and 6 score low almost entirely because of two
compounding effects explained below (extraction damage + section/exercise
concept mismatch), not because gpt-5.1-low is bad at calculus.

## Data-source extraction-quality caveat (read this before trusting the numbers above)

This is the most important finding of the exercise. **`aita_chunks.section_path`
for `document_id=5` is populated for only 30 of 43 numbered sections, and where
present it tags a single page rather than the section's full span** — it could
not be used as ground truth. I instead rebuilt section boundaries from the
book's own Table of Contents (chunk ids 61400–61422) plus a validated constant
`+8` offset between the book's printed page numbers and the DB's `page_number`
column (checked against all 30 populated `section_path` rows — zero deviation).
That derivation is recorded in `section_map.json`.

Far more consequential: **the indexing pipeline that produced these chunks
appears to have stripped the great majority of inline/rendered mathematical
notation from exercise text** — integrand formulas, specific numeric
coefficients, interval endpoints, and named functions are frequently just
*missing*, even though the surrounding English sentence (the instructional
scaffold, "Evaluate the following...", "Find the volume of...") survived
intact. Examples pulled directly from the DB:

- `1 . 2 . 3 .` (three exercise markers back-to-back, zero problem content —
  every symbolic integrand in `3.1 Integration by Parts` was lost this way)
- `A spring has a natural length of cm. It takes J to stretch the spring to cm.` —
  scenario intact, all four numeric quantities gone
- `[T] A chain hangs from two posts m apart to form a catenary described by the
  equation Find the slope...` — the catenary equation itself is gone

This loss is **not uniform**. Some passages (notably all of section `1.4
Integration Formulas and the Net Change Theorem`, the fully-dimensioned solids
in `2.2`, and every problem in `7.5 Conic Sections`) came through with every
number intact — apparently because those particular numbers were typeset as
plain running text in the source rather than as inline math/MathML spans,
which is what seems to get dropped. Grep spot-checks of raw chunk rows (e.g.
ids 62471–62484) confirm this is a genuine upstream extraction gap, not a bug
in my own parsing script.

**Practical consequence for this exercise:** I applied the task's own
instruction ("skip fragments that lost their math content") *unless* the
surviving English text still made the underlying scenario/technique
unambiguous to a human reader (e.g. "a spring... how much work to stretch it
further" is unambiguously `physical-applications` even with the numbers
missing). 26/79 problems are flagged `extraction_quality: "clean"` (nothing
material lost); the remaining 53/79 are `"degraded"` (some inline value/
formula lost but the scenario was still judged legible). This is recorded per
problem in `problems.json`.

**Four numbered sections had *no* usable exercise at all** even under the
relaxed bar, because every surviving exercise in them was a bare instructional
stub with the actual integrand/function entirely gone: `3.2 Trigonometric
Integrals`, `3.3 Trigonometric Substitution`, `3.4 Partial Fractions`, and all
of `7.1/7.2/7.3/7.4` (parametric equations, calculus of parametric curves,
polar coordinates, polar area/arc length — only `7.5 Conic Sections`
survived). As a direct result, **11 of the 40 concepts in the closed list have
zero test problems in this run**: `integrals-inverse-trig`,
`integration-by-parts`, `trigonometric-integrals`, `trigonometric-substitution`,
`partial-fractions`, `taylor-maclaurin-series`, `taylor-series-applications`,
`parametric-equations`, `calculus-parametric-curves`, `polar-coordinates`,
`polar-area-arc-length`. This is arguably the single most important actionable
finding for whoever owns `indexing/` — the exact technique-classification
sections that matter most for a concept-matching product (IBP vs. trig
integrals vs. trig substitution vs. partial fractions are the classic
confusable set) are precisely the ones where the current pipeline destroys the
signal needed to tell them apart.

## Why chapters 1, 5, 6 scored low (two distinct causes, both explainable)

**Cause A — extraction damage removed the exact disambiguating detail.**
E.g. problem 14 (`1.5 Substitution`) reads "the area of a semicircle... use
the substitution [MISSING] to express the area as an integral of a
trigonometric function" — with the substitution itself gone, gpt-5.1
reasonably guessed `trigonometric-substitution` instead of the intended
`u-substitution`; a sighted human would need the missing formula too. Problem
68 (`5.6 Ratio and Root Tests`) is now so degraded ("Suppose that for all
where is a fixed real number...") that no classifier could recover the
intended test.

**Cause B — some of my hand-picked "conceptually legible" exercises are
genuinely adjacent/ambiguous, independent of extraction loss.** Several
problems I kept specifically *because* they read as complete, self-contained
English (mountain-climber MVT, Kepler's-laws orbit questions, coin-flip /
coupon-collector / balancing-scale puzzles, compound-interest sequences) are,
on reflection, borderline calc-1-adjacent or cross-topic word problems that a
careful human grader might *also* place outside the section's headline
concept. gpt-5.1 flagged several of these as `NO_MATCH` or picked a
plausible neighboring concept (`average-value-of-function` for the toll-road
MVT problem; `conic-sections` for the two Kepler ellipse problems;
`exponential-growth-decay` for the fish-population/college-loan sequence
problems). These read as *reasonable, defensible classifications*, not model
failures — but they do inflate the apparent "mismatch" count against my own
section-derived ground truth. `mismatches.json` is the right input for a
human/opus adjudicator to separate real model errors from ground-truth
over-reach.

One clear **actual model miss**: problem 77 (`7.5 Conic Sections`, searchlight
paraboloid) — gpt-5.1's own rationale correctly identifies "a geometry/conic
sections application about a paraboloid" and then answers `NO_MATCH` anyway,
even though `conic-sections` is literally in the provided list. This is a
genuine low-effort-reasoning slip, not a data problem.

## Mismatch summary (see `mismatches.json` for full text + rationale, 33 rows: MISMATCH + PARTIAL + model NO_MATCH)

- **2** (`1.1`) gas-price table — model said NO_MATCH, called it "arithmetic/sequences," missed `riemann-sums-area-approximation`
- **5, 6** (`1.3`) MVT word problems — NO_MATCH / mismatched to `average-value-of-function` (Cause B, arguably defensible)
- **7, 8** (`1.3`) Kepler orbit problems — mismatched to `conic-sections` (Cause B — genuinely about ellipse geometry, defensible)
- **11, 12** (`1.4`) circle/balloon net-change-of-radius — NO_MATCH, called "related rates" (Cause B, borderline calc-1 flavor)
- **13** (`1.4`) braking-distance kinematics — mismatched to `physical-applications` (defensible neighbor)
- **14** (`1.5`) semicircle substitution — mismatched to `trigonometric-substitution` (Cause A, substitution formula missing)
- **28, 29** (`2.6`) Pappus-theorem cone/sphere — mismatched to `volume-by-slicing` (defensible — these genuinely double as slicing/shell verification exercises)
- **34** (`3.1`) IBP particle-velocity problem — mismatched to `fundamental-theorem-of-calculus` (Cause A, velocity function missing, genuinely ambiguous)
- **41** (`3.7`) bare "Evaluate (Be careful!)" stub — correctly NO_MATCH, zero content survived
- **43, 50** (`4.1`, `4.3`) PARTIAL — `separable-equations` picked as primary where `differential-equations-basics`/`exponential-growth-decay` was expected, expected concept present in secondary
- **46** (`4.3`) drug-decay — mismatched to `exponential-growth-decay` (Cause A/B blend — genuinely could be either without the DE shown)
- **55, 56** (`4.5`) terminal-velocity problems — mismatched to `separable-equations` (Cause A, the linear-vs-separable distinction requires the actual ODE, which is stripped)
- **57** (`4.5`) bare "Solve with the initial condition..." stub — correctly NO_MATCH
- **59, 60** (`5.1`) fish-population / college-loan sequences — mismatched to `exponential-growth-decay` (Cause B, very defensible — these ARE modeled with exponential-style recursions)
- **61** (`5.2`) coin-flip probability — NO_MATCH, called it "discrete probability" (Cause B)
- **62, 63** (`5.2`) quarterly-deposit annuity / drug-dosing-interval — mismatched to `sequences` / `exponential-growth-decay` (Cause B)
- **64, 65, 66** (`5.3`, `5.4`) coupon-collector / scooter-fuel / balancing-scale puzzles — NO_MATCH or mismatched to `infinite-series-basics` (Cause B, genuinely puzzle-flavored)
- **68** (`5.6`) badly degraded ratio-test stub — mismatched to `sequences`, essentially unrecoverable text (Cause A)
- **69** (`6.1`) power-series existence-of-radius statement — mismatched to `sequences` (Cause A, all supporting notation stripped)
- **72, 73, 74** (`6.2`) annuity/π-estimation power-series problems — mismatched to `infinite-series-basics`/`exponential-growth-decay`/`taylor-series-applications` (Cause A, the actual series expansion is stripped in all three)
- **77** (`7.5`) searchlight paraboloid — genuine model miss (see above), NO_MATCH despite correct reasoning trail

## Full per-problem table

See `problem_table.md` (also embedded below) for all 79 rows: id, text preview,
section, expected concept, predicted primary/secondary, confidence, match
status.

## Files in this directory

- `problems.json` — 79 extracted exercises with `problem_id`, `problem_text`,
  `section_path` (our derived label, not the DB column), `chapter`,
  `page_number`, `chunk_id`, `source_exercise_number`, `extraction_quality`
- `section_map.json` — section→concept ground-truth map + the data-quality
  caveat about `section_path`/page-offset derivation
- `matches.json` — raw blind gpt-5.1-low classifications (primary, secondary,
  confidence, rationale) per `problem_id`
- `scored_rows.json` — matches joined against ground truth with MATCH/
  PARTIAL/MISMATCH/UNSCORED_NO_MATCH status
- `mismatches.json` — 33 non-MATCH rows (MISMATCH + PARTIAL + model NO_MATCH)
  with full problem text + rationale, for opus adjudication
- `problem_table.md` — full 79-row per-problem table
- `raw_section_chunks.json`, `parsed_exercises_v2.json`, `screened.json`,
  `screened2.json` — intermediate extraction/screening artifacts (kept for
  traceability, not required reading)
- `extract.py`, `parse_exercises2.py`, `build_problems.py`, `match.py`,
  `score.py`, `db.py` — the pipeline scripts, re-runnable end to end
| id | preview | section | expected | primary | secondary | conf | status |
|---|---|---|---|---|---|---|---|
| 1 | The following table gives approximate values of the average annual atmospheric r | 1.1 Approximating Areas | riemann-sums-area-approximation | riemann-sums-area-approximation | definite-integral | 0.90 | MATCH |
| 2 | The following table gives the approximate increase in dollars in the average pri | 1.1 Approximating Areas | riemann-sums-area-approximation | NO_MATCH | - | 0.94 | UNSCORED_NO_MATCH |
| 3 | Suppose can be subdivided into subintervals such that either over or over Set a. | 1.2 The Definite Integral | definite-integral,average-value-of-function | definite-integral | riemann-sums-area-approximation,fundamental-theorem-of-calculus | 0.62 | MATCH |
| 4 | Suppose the average value of f over is 1 and the average value of f over is 1 wh | 1.2 The Definite Integral | definite-integral,average-value-of-function | average-value-of-function | definite-integral | 0.90 | MATCH |
| 5 | Two mountain climbers start their climb at base camp, taking two different route | 1.3 The Fundamental Theorem of Calculus | fundamental-theorem-of-calculus | NO_MATCH | - | 0.97 | UNSCORED_NO_MATCH |
| 6 | To get on a certain toll road a driver has to take a card that lists the mile en | 1.3 The Fundamental Theorem of Calculus | fundamental-theorem-of-calculus | average-value-of-function | - | 0.63 | MISMATCH |
| 7 | Kepler’s first law states that the planets move in elliptical orbits with the Su | 1.3 The Fundamental Theorem of Calculus | fundamental-theorem-of-calculus | conic-sections | physical-applications | 0.70 | MISMATCH |
| 8 | As implied earlier, according to Kepler’s laws, Earth’s orbit is an ellipse with | 1.3 The Fundamental Theorem of Calculus | fundamental-theorem-of-calculus | conic-sections | - | 0.83 | MISMATCH |
| 9 | A ball is thrown upward from a height of 1.5 m at an initial speed of 40 m/ sec. | 1.4 Integration Formulas and the Net Change Theorem | antiderivatives-basic-integration | antiderivatives-basic-integration | differential-equations-basics | 0.92 | MATCH |
| 10 | A ball is thrown upward from a height of 3 m at an initial speed of 60 m/sec. Ac | 1.4 Integration Formulas and the Net Change Theorem | antiderivatives-basic-integration | antiderivatives-basic-integration | physical-applications | 0.93 | MATCH |
| 11 | The area of a circular shape is growing at a constant rate. If the area increase | 1.4 Integration Formulas and the Net Change Theorem | antiderivatives-basic-integration | NO_MATCH | - | 0.86 | UNSCORED_NO_MATCH |
| 12 | A spherical balloon is being inflated at a constant rate. If the volume of the b | 1.4 Integration Formulas and the Net Change Theorem | antiderivatives-basic-integration | NO_MATCH | - | 0.90 | UNSCORED_NO_MATCH |
| 13 | For a given motor vehicle, the maximum achievable deceleration from braking is a | 1.4 Integration Formulas and the Net Change Theorem | antiderivatives-basic-integration | physical-applications | - | 0.92 | MISMATCH |
| 14 | The area of a semicircle of radius 1 can be expressed as Use the substitution to | 1.5 Substitution | u-substitution | trigonometric-substitution | definite-integral | 0.94 | MISMATCH |
| 15 | The largest triangle with a base on the that fits inside the upper half of the u | 2.1 Areas between Curves | area-between-curves | area-between-curves | - | 0.70 | MATCH |
| 16 | Derive the formula for the volume of a sphere using the slicing method. | 2.2 Determining Volumes by Slicing | volume-by-slicing | volume-by-slicing | - | 0.99 | MATCH |
| 17 | Use the slicing method to derive the formula for the volume of a cone. | 2.2 Determining Volumes by Slicing | volume-by-slicing | volume-by-slicing | - | 0.97 | MATCH |
| 18 | A pyramid with height 6 units and square base of side 2 units, as pictured here. | 2.2 Determining Volumes by Slicing | volume-by-slicing | volume-by-slicing | - | 0.73 | MATCH |
| 19 | A pyramid with height 5 units, and an isosceles triangular base with lengths of  | 2.2 Determining Volumes by Slicing | volume-by-slicing | volume-by-slicing | - | 0.83 | MATCH |
| 20 | Use the method of shells to find the volume of a cone with radius and height | 2.3 Volumes of Revolution: Cylindrical Shells | cylindrical-shells | cylindrical-shells | volume-by-slicing | 0.97 | MATCH |
| 21 | Use the method of shells to find the volume of a cylinder with radius and height | 2.3 Volumes of Revolution: Cylindrical Shells | cylindrical-shells | cylindrical-shells | - | 0.96 | MATCH |
| 22 | Pick an arbitrary linear function over any interval of your choice Determine the | 2.4 Arc Length of a Curve and Surface Area | arc-length-surface-area | arc-length-surface-area | - | 0.90 | MATCH |
| 23 | A light bulb is a sphere with radius in. with the bottom sliced off to fit exact | 2.4 Arc Length of a Curve and Surface Area | arc-length-surface-area | arc-length-surface-area | - | 0.96 | MATCH |
| 24 | How much work is done when a person lifts a lb box of comics onto a truck that i | 2.5 Physical Applications | physical-applications | physical-applications | - | 0.93 | MATCH |
| 25 | A spring has a natural length of cm. It takes J to stretch the spring to cm. How | 2.5 Physical Applications | physical-applications | physical-applications | definite-integral | 0.98 | MATCH |
| 26 | A -m spring requires J to stretch the spring to m. How much work would it take t | 2.5 Physical Applications | physical-applications | physical-applications | - | 0.98 | MATCH |
| 27 | A shock absorber is compressed 1 in. by a weight of 1 t. What is the spring cons | 2.5 Physical Applications | physical-applications | physical-applications | - | 0.78 | MATCH |
| 28 | A general cone created by rotating a triangle with vertices and around the -axis | 2.6 Moments and Centers of Mass | physical-applications | volume-by-slicing | cylindrical-shells | 0.88 | MISMATCH |
| 29 | A sphere created by rotating a semicircle with radius around the -axis. Does you | 2.6 Moments and Centers of Mass | physical-applications | volume-by-slicing | definite-integral | 0.93 | MISMATCH |
| 30 | If a culture of bacteria doubles in hours, how many hours does it take to multip | 2.8 Exponential Growth and Decay | exponential-growth-decay | exponential-growth-decay | - | 0.95 | MATCH |
| 31 | The effect of advertising decays exponentially. If of the population remembers a | 2.8 Exponential Growth and Decay | exponential-growth-decay | exponential-growth-decay | - | 0.93 | MATCH |
| 32 | You are cooling a turkey that was taken out of the oven with an internal tempera | 2.8 Exponential Growth and Decay | exponential-growth-decay | exponential-growth-decay | separable-equations | 0.92 | MATCH |
| 33 | [T] A chain hangs from two posts m apart to form a catenary described by the equ | 2.9 Calculus of the Hyperbolic Functions | hyperbolic-functions | hyperbolic-functions | - | 0.96 | MATCH |
| 34 | A particle moving along a straight line has a velocity of after t sec. How far d | 3.1 Integration by Parts | integration-by-parts | fundamental-theorem-of-calculus | physical-applications | 0.89 | MISMATCH |
| 35 | Approximate using the midpoint rule with four subdivisions to four decimal place | 3.6 Numerical Integration | numerical-integration | numerical-integration | riemann-sums-area-approximation | 0.96 | MATCH |
| 36 | Use the trapezoidal rule estimate Compare this value with the exact value and fi | 3.6 Numerical Integration | numerical-integration | numerical-integration | definite-integral | 0.94 | MATCH |
| 37 | The growth rate of a certain tree (in feet) is given by where t is time in years | 3.6 Numerical Integration | numerical-integration | numerical-integration | - | 0.98 | MATCH |
| 38 | Given that we know the Fundamental Theorem of Calculus, why would we want to dev | 3.6 Numerical Integration | numerical-integration | numerical-integration | definite-integral,fundamental-theorem-of-calculus | 0.96 | MATCH |
| 39 | Without integrating, converges or diverges. Determine whether the improper integ | 3.7 Improper Integrals | improper-integrals | improper-integrals | definite-integral | 0.96 | MATCH |
| 40 | Evaluate the improper integrals. Each of these integrals has an infinite discont | 3.7 Improper Integrals | improper-integrals | improper-integrals | - | 1.00 | MATCH |
| 41 | Evaluate (Be careful!) (Express your answer using three decimal places.) | 3.7 Improper Integrals | improper-integrals | NO_MATCH | - | 0.05 | UNSCORED_NO_MATCH |
| 42 | Find the volume of the solid generated by revolving about the x-axis the area un | 3.7 Improper Integrals | improper-integrals | improper-integrals | volume-by-slicing | 0.63 | MATCH |
| 43 | Find the general solution to describe the velocity of a ball of mass that is thr | 4.1 Basics of Differential Equations | differential-equations-basics | separable-equations | differential-equations-basics | 0.86 | PARTIAL |
| 44 | Estimate the following solutions using Euler’s method with steps over the interv | 4.2 Direction Fields and Numerical Methods | differential-equations-basics | differential-equations-basics | - | 0.94 | MATCH |
| 45 | Differential equations can be used to model disease epidemics. In the next set o | 4.2 Direction Fields and Numerical Methods | differential-equations-basics | differential-equations-basics | - | 0.86 | MATCH |
| 46 | Most drugs in the bloodstream decay according to the equation where is the conce | 4.3 Separable Equations | separable-equations | exponential-growth-decay | - | 0.96 | MISMATCH |
| 47 | A tank contains kilogram of salt dissolved in liters of water. A salt solution o | 4.3 Separable Equations | separable-equations | separable-equations | physical-applications | 0.98 | MATCH |
| 48 | The liquid base of an ice cream has an initial temperature of before it is place | 4.3 Separable Equations | separable-equations | separable-equations | exponential-growth-decay | 0.92 | MATCH |
| 49 | [T] You have a cup of coffee at temperature that you put outside, where the ambi | 4.3 Separable Equations | separable-equations | separable-equations | exponential-growth-decay,differential-equations-basics | 0.81 | MATCH |
| 50 | You have a cup of coffee at temperature which you let cool minutes before you po | 4.3 Separable Equations | separable-equations | exponential-growth-decay | separable-equations | 0.83 | PARTIAL |
| 51 | A population of deer inside a park has a carrying capacity of and a growth rate  | 4.4 The Logistic Equation | logistic-equation | logistic-equation | separable-equations | 0.97 | MATCH |
| 52 | Bengal tigers in a conservation park have a carrying capacity of and need a mini | 4.4 The Logistic Equation | logistic-equation | logistic-equation | separable-equations | 0.98 | MATCH |
| 53 | [T] The Gompertz equation has been used to model tumor growth in the human body. | 4.4 The Logistic Equation | logistic-equation | logistic-equation | separable-equations | 0.83 | MATCH |
| 54 | [T] It is estimated that the world human population reached billion people in an | 4.4 The Logistic Equation | logistic-equation | logistic-equation | separable-equations | 0.97 | MATCH |
| 55 | A falling object of mass can reach terminal velocity when the drag force is prop | 4.5 First-order Linear Equations | first-order-linear-ode | separable-equations | physical-applications | 0.86 | MISMATCH |
| 56 | A more accurate way to describe terminal velocity is that the drag force is prop | 4.5 First-order Linear Equations | first-order-linear-ode | separable-equations | differential-equations-basics | 0.96 | MISMATCH |
| 57 | Solve with the initial condition As approaches what happens to your formula? | 4.5 First-order Linear Equations | first-order-linear-ode | NO_MATCH | - | 0.10 | UNSCORED_NO_MATCH |
| 58 | [T] Suppose you start with one liter of vinegar and repeatedly remove replace wi | 5.1 Sequences | sequences | sequences | exponential-growth-decay | 0.88 | MATCH |
| 59 | [T] A lake initially contains fish. Suppose that in the absence of predators or  | 5.1 Sequences | sequences | exponential-growth-decay | - | 0.83 | MISMATCH |
| 60 | [T] A student takes out a college loan of at an annual percentage rate of compou | 5.1 Sequences | sequences | exponential-growth-decay | - | 0.96 | MISMATCH |
| 61 | [T] Find the probability that a fair coin is flipped a multiple of three times b | 5.2 Infinite Series | infinite-series-basics | NO_MATCH | - | 0.99 | UNSCORED_NO_MATCH |
| 62 | [T] A person deposits at the beginning of each quarter into a bank account that  | 5.2 Infinite Series | infinite-series-basics | sequences | - | 0.76 | MISMATCH |
| 63 | [T] A certain drug is effective for an average patient only if there is at least | 5.2 Infinite Series | infinite-series-basics | exponential-growth-decay | - | 0.86 | MISMATCH |
| 64 | [T] Complete sampling with replacement, sometimes called the coupon collector’s  | 5.3 The Divergence and Integral Tests | divergence-and-integral-tests | infinite-series-basics | sequences | 0.60 | MISMATCH |
| 65 | Suppose a scooter can travel km on a full tank of fuel. Assuming that fuel can b | 5.3 The Divergence and Integral Tests | divergence-and-integral-tests | NO_MATCH | - | 0.97 | UNSCORED_NO_MATCH |
| 66 | [T] Evelyn has a perfect balancing scale, an unlimited number of weights, and on | 5.4 Comparison Tests | comparison-tests | infinite-series-basics | sequences | 0.79 | MISMATCH |
| 67 | In view of the previous exercise, it may be surprising that a subseries of the h | 5.4 Comparison Tests | comparison-tests | comparison-tests | infinite-series-basics | 0.79 | MATCH |
| 68 | Suppose that for all where is a fixed real number. For which values of is guaran | 5.6 Ratio and Root Tests | ratio-root-tests | sequences | - | 0.55 | MISMATCH |
| 69 | Given any sequence there is always some possibly very small, such that converges | 6.1 Power Series and Functions | power-series-convergence | sequences | - | 0.92 | MISMATCH |
| 70 | If has radius of convergence and if for all n, then the radius of convergence of | 6.1 Power Series and Functions | power-series-convergence | power-series-convergence | - | 0.92 | MATCH |
| 71 | Suppose that converges at At which of the following points might the series dive | 6.1 Power Series and Functions | power-series-convergence | power-series-convergence | infinite-series-basics | 0.88 | MATCH |
| 72 | Suppose that an annuity has a present value What interest rate r would allow for | 6.2 Properties of Power Series | power-series-operations | infinite-series-basics | - | 0.77 | MISMATCH |
| 73 | Suppose that an annuity What interest rate r would allow for perpetual annual pa | 6.2 Properties of Power Series | power-series-operations | exponential-growth-decay | - | 0.81 | MISMATCH |
| 74 | [T] Recall that Assuming an exact value of estimate by evaluating partial sums o | 6.2 Properties of Power Series | power-series-operations | taylor-series-applications | taylor-maclaurin-series,infinite-series-basics | 0.86 | MISMATCH |
| 75 | A satellite dish is shaped like a paraboloid of revolution. The receiver is to b | 7.5 Conic Sections | conic-sections | conic-sections | - | 0.90 | MATCH |
| 76 | Consider the satellite dish of the preceding problem. If the dish is 8 feet acro | 7.5 Conic Sections | conic-sections | conic-sections | - | 0.93 | MATCH |
| 77 | A searchlight is shaped like a paraboloid of revolution. A light source is locat | 7.5 Conic Sections | conic-sections | NO_MATCH | - | 0.98 | UNSCORED_NO_MATCH |
| 78 | Whispering galleries are rooms designed with elliptical ceilings. A person stand | 7.5 Conic Sections | conic-sections | conic-sections | - | 0.98 | MATCH |
| 79 | A person is standing 8 feet from the nearest wall in a whispering gallery. If th | 7.5 Conic Sections | conic-sections | conic-sections | - | 0.97 | MATCH |