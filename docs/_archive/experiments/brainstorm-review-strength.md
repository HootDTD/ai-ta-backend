# Review — Strength of the "Direction C: Graded Claim Alignment" brainstorm

**Date:** 2026-06-23
**Reviewer role:** senior systems architect, critical evaluation
**Inputs:** `docs/brainstorm-for-missgrading.md` (the proposal),
`docs/APOLLO-EDGE-LOSS-FINDINGS.md` (the evidence-backed diagnosis), and the live
Apollo source under `apollo/resolution/**` + `apollo/graph_compare/**`.
**Scope:** analysis only. No source modified.

---

## Verdict

**Direction C is the correct *diagnosis* wrapped around an *over-scoped* v1
prescription.** Its central insight — that Apollo conflates "can I name this
node?" with "is this structural claim correct?", and gates the second on the
first decided per-node in isolation — is exactly right and is corroborated by the
code: `build_student_canonical` drops any edge with an unresolved endpoint
(`canonical.py:275`), and the scorer keys every dimension on exact resolved
canonical triples (`scores.py:95-101`, `core.py:180`), so an unnameable endpoint
is structurally invisible regardless of whether the claim is true. The proposal's
ambition, however, runs well past the diagnosed problem: it reframes the entire
grading representation around a multi-score `ClaimAlignment` object, per-node
hand-authored "semantic contracts", and LLM semantic judges, when the measured
loss (40/100 edges, 93% from one failure mode — `FINDINGS §"The quantification"`)
is recoverable by a much smaller change. The good news is that C is *staged*, and
its early steps (1-3) are essentially Direction B with better bookkeeping and are
genuinely worth doing. The later steps (4-5, the semantic-contract + LLM-judge
core) are where the cost, calibration burden, and the project's
deterministic/auditable/95%-patch-coverage constraints bite hardest, and they are
not justified by the v1 evidence.

**Rating: Mixed (leaning Strong-with-reservations for Steps 1-3 only; Weak for
Steps 4-6 as a v1 commitment).**

---

## Does it solve the stated problem?

The findings doc quantifies the loss precisely (`FINDINGS §"The quantification"`,
`§"Mode 1 split"`): 40/100 student edges dropped; Mode 1 (endpoint unresolved) is
93% of all edge-fidelity loss, split into **1a** (31 incidents — derived/solved/
numeric equation forms) and **1b** (17 incidents — Condition 11 / Simplification 3
/ Definition 2 / ProcedureStep 1). Problems 3/4/5 together are ~3 of ~43 loss
events.

- **1a (derived equation forms): SOLVED in principle, and the strongest part of
  the design.** Step 3's deterministic match ladder (`exact_symbolic` →
  `symbolic_equivalent` → `solved_form_of` → `numeric_instance_of` →
  `final_value_match`) targets the single biggest bucket. Critically, this is
  *additive to* an existing seam the brainstorm under-credits: `problem_inputs.py:65`
  (`_collect_symbolic_mappings`) already harvests per-simplification
  `content.substitution` maps into the symbolic tier — that is "Direction A,"
  already merged (PR #63 per project memory), and it is narrow exactly as the
  findings doc says (`FINDINGS:152-156`): it only collapses a *declared* derived
  form, not an arbitrary `solve()`-reached one. A `solved_form_of` checker (run
  SymPy `solve()` on the reference equation for each free variable and compare
  zero-forms) is the right generalization and is deterministic. **This is the part
  of C that most clearly earns its place.**

- **1b (non-equation nodes): PARTIALLY addressed, and this is where C is both
  most needed and most expensive.** The root cause is code-confirmed: every
  reference candidate is built with `aliases=()` (`candidates.py:101`), so a
  free-text condition/simplification/definition can only resolve via `exact`,
  `fuzzy ≥ 0.9` token_set_ratio, or the one LLM adjudication call — and only
  misconceptions carry alias surface forms (`trigger_phrases`, `candidates.py:133`).
  C's answer (Step 4 semantic contracts + an `entails/partially_entails/
  contradicts/unrelated` judge) *would* cover 1b, but it is the heaviest machinery
  in the proposal and the part most exposed to the "LLM vibes" risk the brainstorm
  itself flags (Guardrail items 6, 8). A far cheaper option — populate `aliases`
  for reference conditions/simplifications, the channel that already exists and is
  already consumed by `match_alias_all`/`match_fuzzy_all` (`tiers.py:210,269`) —
  would recover a large share of the 17 1b incidents with zero new architecture.
  The brainstorm explicitly argues "aliases are not enough" (`brainstorm:240-276`),
  and it is *right that aliases can't express graded partial credit* — but it is
  answering a question (partial-credit gradation) that the v1 evidence did not
  ask. The 1b loss is binary disappearance, not mis-graded partial credit.

- **Problem 3 (over-merge → PRECEDES self-loop): NOT addressed.** This is a
  resolver many-to-one defect (`assignment.py:50` allows many student nodes → one
  candidate; two sequential procedure steps collapse and `canonical.py` merges
  them so the PRECEDES edge becomes `from_key == to_key`). C's claim layer sits
  *downstream* of resolution and does not touch the many-to-one rule, so the
  self-loop still manufactures itself. The brainstorm does not mention it.

- **Problem 4 (cross-type LLM resolution + false merge-type invariant): NOT
  addressed, and arguably worsened in spirit.** The HARD type gate lives in the
  content tiers (`structural.py:42`) but the LLM adjudication path
  (`adjudication.py:90-121`) never re-applies `type_compatible` — confirmed: it
  maps `returned_key` straight through `by_key` with no type check. C *keeps*
  strict type compat for identity resolution (Guardrail 4-5) but then introduces a
  *second* LLM judge (the semantic-contract judge) that is explicitly allowed to be
  "cross-form" (Guardrail 6). Unless that judge is itself type-gated per candidate,
  C adds a second place where the type invariant can leak. The latent
  `member_nodes[0].node_type` bug (`canonical.py:248-249`) is untouched.

- **Problem 5 (DEPENDS_ON type/direction drift): marginally addressed.** Graded
  edge alignment with endpoint scoring could absorb direction drift if the matcher
  is made direction-loose for typed edges, but `_dependency` is *already*
  direction-loose (`scores.py:138-155`, `frozenset` key-pairs); the drift that
  bites is in *typed* edges (`core.py:180` is direction-exact). C doesn't
  specifically target this, and the findings doc says deprioritize it anyway.

**Net coverage:** C fully covers 1a (the biggest bucket), covers 1b but with the
most expensive instrument when a cheap one exists, and leaves 3/4/5 — which the
findings doc correctly flags as *separate resolver fixes*, not matcher fixes
(`FINDINGS:313-318`). So C does *not* "solve the diagnosed problem" end-to-end; it
solves the 93% edge-loss bucket and explicitly defers the resolver-quality bugs,
same as Direction B.

---

## Architectural soundness

The brainstorm's model of the current pipeline is **accurate**. Verified against
code:

- **The scorer is exact-match and resolution-gated.** Edge scoring is
  `{(e.edge_type, e.from_key, e.to_key) for e in student.edges}` set membership
  (`core.py:180`); every sub-score keys on the same triple or a key-pair
  (`scores.py:95-101` for `_edge_coverage`, `:142-154` for `_dependency`). Node
  coverage is set membership of `canonical_key` (`coverage.py:40-44`). There is no
  fuzzy or graded layer anywhere in the scorer.
- **The drop happens before scoring, in canonicalization.** `build_student_canonical`
  drops an edge when `from_key is None or to_key is None` and only increments
  `dropped_edge_count` (`canonical.py:275-277`). Unresolved nodes are retained as
  `unresolved_nodes` (`canonical.py:288-295`) **but I confirmed they never reach
  the scorer**: `scores.py` imports `CanonicalGraph` and reads `student.edges` /
  `student.nodes` only — neither `unresolved_nodes` nor `dropped_edge_count` is
  referenced in `scores.py` or `coverage.py`. They feed `core.py::_emit_findings`
  (`core.py:155-158`) as *diagnostics only*. So the brainstorm's "edge touching it
  is dropped → scorer never sees the structural claim → usage/edge coverage
  collapse" (`brainstorm:88-94`) is literally true.

**Where would the new layer plug in, and is it realistic?** This is C's biggest
unstated architectural problem. The grading core is deliberately **pure: no Neo4j,
no resolver, no LLM, no Postgres** (`core.py:1-19`, "PURE inputs"; `canonical.py:22-24`).
`grade_attempt(student_canonical, reference_graph)` consumes two frozen dataclasses
and is called from `done_grading.py:228` *after* `resolve → write_resolution →
build_student_canonical`. C's claim layer needs the raw student surfaces, the
candidate set, SymPy, and (for Step 4) an LLM — none of which the pure core has.
So C lands in one of two places, and the brainstorm never picks:

1. **Between resolution and canonicalization** — produce `ClaimAlignment`s, then
   let `build_student_canonical` keep one-anchor edges whose unresolved endpoint
   has a high-confidence alignment. This is the *least invasive* option and maps
   cleanly onto Steps 1-2. It keeps the scorer pure: alignment becomes "soft
   resolution" that fills `resolved_key_by_node` (or a parallel map) so the
   existing exact-match scorer still runs unchanged. **This is feasible today** and
   is essentially Direction B.

2. **Replacing the scorer's exact-match core with alignment-weighted scoring**
   (Step 5: "node coverage = max alignment score", "edge usage = score of best
   aligned USES claim"). This **breaks the purity contract** and the
   determinism/reproducibility guarantee that `core.py:16-19` and every `scores.py`
   docstring lean on, and it rewrites `coverage.py` (set membership → max over
   graded alignments) and `_edge_coverage` (boolean triple match → weighted
   endpoint product). This is a substantial rewrite of the most-tested, most-frozen
   module in the subsystem, behind a v1 flag that isn't even live yet
   (`APOLLO_GRAPH_SIM_LIVE_ENABLED` default OFF everywhere, per `apollo.md`).

The brainstorm's Step 5 implies option 2 ("Let node coverage and edge coverage be
computed from alignment scores, not only exact resolved keys"), but its Steps 1-2
imply option 1. **That ambiguity is the single most important thing to resolve
before any code is written.** Option 1 is a bolt-on; option 2 is a core rewrite.
The proposal reads as if they're the same project. They are not.

**Reference side is fine.** `build_reference_canonical` (`canonical.py:111-207`)
already emits USES/PRECEDES/DEPENDS_ON targets from `reference_solution`, so there
*are* reference edges to align against — the "structural scoring dead by
construction" regression is already fixed (`canonical.py:162-167`). A claim layer
has real reference structure to score against. Good.

---

## Direction A vs B vs C — is C justified?

The findings doc frames the choice precisely (`FINDINGS:307-318`): A resolves
derived equations (1a only); B keeps unresolved nodes first-class + a tolerant
per-edge matcher (1a + 1b); neither fixes 3/4 (separate resolver fixes).

**C is Direction B plus a representational reframing.** Strip the prose and C's
*mechanism* is B: keep the dropped edge, recover it via the resolved anchor + a
tolerant endpoint match (Steps 1-2 are verbatim B). What C adds on top of B is:
(a) a multi-field `ClaimAlignment` object (correctness/completeness/contradiction/
confidence + basis + missing/wrong parts), (b) per-node semantic contracts, (c) an
LLM judge for non-equation entailment, (d) misconception-as-output-of-alignment.

Is the extra machinery earning its keep *for the diagnosed problem*? **Mostly
not, at v1.** The measured failure is **binary disappearance of correct claims**,
not **mis-quantified partial credit**. Every dropped edge in the findings doc is a
claim the student got *right* that scored *zero*. Recovering it to a *boolean*
"this edge is supported" already moves `edge_coverage`/`usage`/`scoping` off the
floor. The decomposed correctness scores, partial-credit rules, and contradiction
scores are answers to a *different, harder* problem (fine-grained partial credit
and misconception synthesis) that the evidence has not yet shown Apollo needs and
that the current rubric can't even consume — `scores.py` produces ratios in [0,1]
from boolean edge matches; there is no consumer for a 0.6 "partially entails"
today. C is solving for a grading sophistication the rest of the system isn't
built to use yet.

**Is C better than "just add aliases"?** For 1a, yes — aliases can't express
`realGDP = nomGDP/(PI/100)` as a solved form; you need symbolic/numeric checks
(Step 3). For 1b, *adding aliases is probably the better v1 move*: it uses the
existing `match_alias_all`/`match_fuzzy_all` channel (`tiers.py:210-302`), is
deterministic, needs no new object, and directly attacks the `aliases=()` root
cause (`candidates.py:101`). The brainstorm's argument against aliases
(`brainstorm:251`) is about *gradation*, which is not the v1 failure.

**So C is justified only in part:** its Step 3 (equation alignment) is a genuine
advance over both A and "add aliases"; its Steps 4-5 (semantic contracts + judge +
alignment-weighted scoring) are B with a large, mostly-unjustified-by-v1-evidence
superstructure.

---

## Cost, complexity, and authoring burden

This is where C is weakest as a v1 commitment.

- **Added LLM calls / latency.** Today the entire resolution path makes **at most
  ONE** LLM call per attempt (`resolver.py:178-184`, `adjudication.py` — "ONE
  `main_chat` call per attempt MAX"). Step 4's semantic-contract judge is, by
  construction, a *per-(claim × candidate-contract)* judgment. Even narrowed to the
  closed candidate set (~15-25 candidates, `candidates.py:7`) and only for
  non-equation unresolved nodes, this is a structural jump from 1 call to N calls,
  on the synchronous Done path. The brainstorm's Guardrail 3 ("LLM judges operate
  only after candidate narrowing") helps but does not restore the 1-call ceiling.
  Apollo's whole resolver design philosophy is "a tiny matching problem, not a
  search" (`candidates.py:7-8`); C reintroduces an LLM-per-candidate cost the
  current design deliberately avoided.
- **Authoring burden.** Semantic contracts (`canonical_claim`, `required_meaning[]`,
  `partial_credit_rules[]`, `contradictions[]`, per-equation `partial_error_patterns[]`)
  are **hand-authored per reference node per problem**. Apollo currently authors
  `reference_solution` steps with `entity_key`, `symbolic`, `depends_on`,
  `uses_equations`, `order`, `substitution` — already non-trivial. Contracts
  roughly double the authoring surface and shift it from *structural* (checkable)
  to *semantic* (judgment-laden, drift-prone). Open question 5 ("hand-authored vs
  auto-generated vs curated") is left unanswered, and it is the difference between
  "feasible" and "a content-ops project." Auto-generating contracts from reference
  nodes and curating them is plausible but is itself a sub-project with its own LLM
  cost and review loop.
- **Calibration.** Decomposed scores (correctness/completeness/contradiction) and
  the threshold ladder (open question 7) need a labeled set to calibrate, and
  Apollo's calibration harness today compares shadow-vs-OLD letter agreement
  (`done_grading.py:280-293`) — it is not a per-claim threshold tuner. The
  brainstorm's Step 6 acknowledges this but underestimates that calibrating a
  multi-dimensional graded judge is a standing maintenance cost, not a one-time
  step.
- **Over-engineered for v1:** `completeness_score` + `contradiction_score` +
  `confidence` as *separate* persisted dimensions, the `candidate_misconception`
  synthesis pipeline (`brainstorm:699-708`), and n-best/probabilistic resolution
  (open question 3) are all defensible *eventually* but are not what the 40%-loss
  evidence calls for. The misconception-synthesis idea in particular is a whole
  separate research line (it intersects the deferred misconception trust-gradient
  work in project memory) and should not be smuggled into an edge-recovery fix.

**Payoff vs cost:** Steps 1-3 are cheap (deterministic, no new LLM calls, no new
authoring) and recover the 1a bucket (31/48 incidents) plus give 1b a survival
path. That payoff is high and the cost is low. Steps 4-5 carry most of the cost
and the residual risk, for the 1b bucket (17 incidents) that a cheaper alias fix
substantially addresses. The cost curve and the value curve diverge sharply after
Step 3.

---

## Staged path assessment

The staging is the proposal's saving grace — it is correctly ordered and each
early step delivers value independently.

- **Step 1 (preserve dropped edges as claims, log only): low risk, high value,
  do it first.** It is observability with zero scoring change. It also directly
  remedies a current information loss — `dropped_edge_count` is a bare integer
  (`canonical.py:86`); you can't even see *which* edges died without the Cypher
  probes in the findings appendix. Strongly endorse.
- **Step 2 (one-anchor edge recovery, constrained to the resolved anchor's
  expected edges): the highest-leverage single step.** This is the precise
  inversion the diagnosis calls for, *with* a built-in false-positive bound (only
  compare the unresolved endpoint against candidates the resolved anchor is
  expected to relate to). It maps onto plug-in option 1 (soft-fill the resolved
  map, keep the scorer pure). This is where I would concentrate effort.
- **Step 3 (equation claim alignment, deterministic ladder): do it, it's the 1a
  fix.** Deterministic, testable to 95% patch coverage, no LLM. `solved_form_of`
  via SymPy `solve()` is the real generalization of the existing
  `_collect_symbolic_mappings` seam.
- **Step 4 (semantic contracts + LLM judge for non-equations): defer; pilot
  aliases first.** Highest cost, highest authoring burden, the LLM-vibes risk.
  Before building it, populate reference `aliases` (cheap, attacks `candidates.py:101`
  directly) and measure how much of the 17-incident 1b bucket that alone recovers.
- **Step 5 (feed alignments into node/edge scores): the fork in the road.** If
  this means "soft-fill resolution and keep the exact scorer" — fine, low risk. If
  it means "rewrite coverage/edge_coverage as alignment-weighted" — that is a
  core-purity-breaking rewrite that should be its own milestone with its own design
  review, not Step 5 of a brainstorm.
- **Step 6 (audit + calibrate before student-facing): correct and mandatory.**
  Aligns with the existing shadow→calibration→promotion machinery
  (`done_grading.py`, the dormant LIVE flag). Good.

**Risk concentration:** ~80% of the risk and cost sits in Steps 4-5. Steps 1-3 are
a clean, shippable increment that resolves the majority bucket. The staging lets
you stop after Step 3 and re-evaluate — which is exactly what I'd recommend.

---

## Project constraints

- **Deterministic-where-possible:** Steps 1-3 are fully deterministic and honor
  the resolver's existing "deterministic given a deterministic adjudicator"
  contract (`resolver.py:16-19`). Step 4's per-candidate LLM judge erodes this; it
  must at minimum be temperature-0 and logged, and even then it is judgment, not
  computation. The current design's single-call discipline is a feature C should
  preserve, not relax.
- **Auditability:** C is strong here in intent — every alignment carries
  `basis`/`method`/`missing`/`wrong` (Guardrail 7), which is richer than today's
  bare `unresolved_finding(node_id, surface)` (`core.py:155-158`). This is a real
  improvement and should be retained even in the reduced (Steps 1-3) version: a
  recovered one-anchor edge should log its anchor, the matched reference edge, and
  the method.
- **95% patch coverage (CLAUDE.md contract):** Steps 1-3 are unit-testable to that
  bar (pure functions, deterministic) and the existing test layout already has the
  homes for them (`graph_compare/tests/test_scores.py`, `test_student_canonical.py`,
  `resolution/tests/test_resolver.py`, and the existing acceptance file
  `test_derived_equation_resolution.py`). Step 4's LLM judge is the usual
  hard-to-cover surface: you cover the deterministic plumbing and stub the judge,
  which means the *judgment* itself is exercised only by the (non-coverage-counted)
  corpus — acceptable but it shifts real confidence onto the calibration corpus,
  not the unit gate.
- **The brainstorm's own "don't make it an unconstrained LLM vibes system"
  guardrail:** the proposal states this (line 716) and then proposes the exact
  mechanism (a semantic judge over hand-authored contracts) that most strains it.
  The guardrails (closed candidate set, deterministic-first, type-gate retained)
  are the right fences, but Steps 1-3 honor them almost for free while Steps 4-5
  rely on them being implemented perfectly. The safest reading of the brainstorm's
  own guardrails is "do Steps 1-3."

---

## Strongest aspects

1. **The core diagnosis is correct and code-confirmed.** "Grades names, not
   meaning; gates the claim on per-node naming decided in isolation" is exactly
   what `canonical.py:275` + `scores.py` + `core.py:180` do. The inversion framing
   (evaluate alignment, *then* decide graph credit) is the right conceptual move.
2. **Step 2 (one-anchor edge recovery with the anchor-constrained candidate set)
   is the precise, bounded fix the evidence calls for** — it recovers dropped edges
   *and* contains false positives by construction, and it slots into the pipeline
   without breaking the pure scorer.
3. **Step 3's deterministic equation-alignment ladder is a genuine advance** over
   both Direction A and "add aliases," and is the right generalization of the
   existing `_collect_symbolic_mappings` seam — deterministic, auditable,
   coverable.
4. **Auditability-by-design** (basis/method/missing/wrong on every alignment) is a
   real upgrade over today's bare unresolved findings and should survive into any
   reduced version.
5. **The staging is honest and correctly ordered**, with value front-loaded and a
   natural stop point after Step 3.

## Weakest aspects

1. **Scope creep past the evidence.** The measured problem is binary
   disappearance of *correct* claims (40 edges, 93% one mode). C re-architects for
   fine-grained partial credit + misconception synthesis, which the evidence
   doesn't demand and the current rubric can't consume.
2. **The plug-in point is ambiguous and the harder reading breaks a load-bearing
   contract.** Step 5 as "alignment-weighted scoring" rewrites the pure,
   deterministic, heavily-frozen `core.py`/`coverage.py`/`scores.py`. The brainstorm
   treats the bolt-on (option 1) and the core rewrite (option 2) as one project.
3. **Cost/authoring/calibration of Steps 4-5.** From 1 LLM call/attempt to
   N-per-candidate; hand-authored semantic contracts roughly double a non-trivial
   authoring surface and make it judgment-laden; multi-dimensional thresholds need a
   standing calibration loop. The payoff (the 17-incident 1b bucket) is largely
   reachable by the much cheaper alias fix C dismisses too quickly.
4. **Leaves Problems 3 and 4 untouched (and may add a second type-gate leak).**
   Over-merge (`assignment.py:50`), the cross-type LLM path (`adjudication.py`
   bypasses `structural.py:42`), and the false `member_nodes[0].node_type` invariant
   (`canonical.py:248-249`) are all downstream-of or orthogonal-to the claim layer
   and need separate resolver fixes the proposal doesn't make.
5. **Self-inconsistent on its own guardrail** — declares "not an unconstrained LLM
   vibes system" while its differentiating core (Step 4) is the one part that most
   strains determinism and auditability.

---

## Recommendation

**Adopt a reduced Direction C: ship Steps 1, 2, and 3 only; treat that as the
realization of Direction B with deterministic equation alignment. Defer Steps 4-5;
pilot reference aliases for the 1b bucket first; spin Step 5's "alignment-weighted
scoring" out into its own design milestone if and only if the evidence after Steps
1-3 still shows uncovered loss.**

Concretely:

1. **Step 1 first (log dropped edges as claims).** Pure observability; turn
   `dropped_edge_count` into a structured `unresolved_edge_claims` artifact carrying
   each dropped edge's type + both endpoint surfaces + which endpoint failed. Zero
   scoring change, immediate diagnostic value.
2. **Step 2 next (one-anchor edge recovery), implemented as soft-fill that keeps
   the scorer pure** — for an edge with one resolved endpoint, attempt to align the
   unresolved endpoint only against candidates the resolved anchor is expected to
   relate to (its reference USES/SCOPES/DEPENDS_ON targets), and on a
   high-confidence alignment record a `resolved_key` for that node so the *existing*
   exact-match scorer credits the edge. This is the highest-leverage step and does
   not touch `core.py`/`coverage.py`/`scores.py`.
3. **Step 3 (deterministic equation ladder)** — add `solved_form_of` (SymPy
   `solve()` on the reference equation per free variable) and `numeric_instance_of`
   (substitute problem givens, compare to the reference zero-form within tolerance)
   to the symbolic tier or the candidate-assembly seam. Lock it with the existing
   `test_derived_equation_resolution.py` acceptance harness.
4. **Before Step 4: populate `aliases` for reference conditions/simplifications/
   definitions** (`candidates.py:101`) and measure 1b recovery. Only if a material
   slice of the 17-incident bucket remains lost should the semantic-contract judge
   be built — and then as an explicitly LLM-gated, separately-logged path per the
   brainstorm's own Guardrails 8-10.
5. **Separately, schedule the resolver-quality fixes** for Problems 3 and 4
   (many-to-one PRECEDES self-loop; LLM type-gate bypass; `member_nodes[0].node_type`
   invariant). These are independent of the claim layer and the findings doc
   correctly flags them as such.

**One-line next step:** open a small design spec for "Step 1+2 soft-fill edge
recovery (keep the scorer pure)" and lock it with a RED acceptance test that
reproduces a one-anchor dropped USES edge (deflator/Bernoulli) and asserts
`usage`/`edge_coverage` move off zero — exactly the `test_derived_equation_resolution.py`
shape, extended to the one-anchor case.
