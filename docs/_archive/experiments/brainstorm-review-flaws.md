# RED-TEAM Review — "Direction C: Graded Claim Alignment" (brainstorm-for-missgrading.md)

**Date:** 2026-06-23
**Reviewer stance:** Adversarial. Sole job is to find where this design breaks if implemented as written.
**Method:** Every claim about "the current system" was checked against the live source under `ai-ta-backend/apollo/`. `file:line` cites throughout.
**Scope note:** This is a GRADING system. A false positive (crediting a vague/wrong answer) is worse than a false negative. The whole thesis of Direction C is "credit more of what currently doesn't resolve," so the burden is on the design to show it does not inflate. It does not meet that burden.

---

## Top flaws — ranked

| # | Flaw | Category | Severity | Why it bites |
|---|------|----------|----------|--------------|
| 1 | Design defeats the only hard quality gate (`unresolved_rate > 0.35` abstention) by construction | New failure mode / scoring | **Critical** | Direction C exists to make unresolved nodes count; the abstention gate exists to STOP grading when too many nodes are unresolved. Inflating credit silently disables the system's one circuit-breaker. |
| 2 | Mischaracterizes the current system: claims unresolved nodes "disappear from structural scoring." Node-coverage + soundness do NOT depend on edge resolution | Misreading | **Critical** | The entire premise ("grades names not meaning") is overstated. Coverage is set-membership over resolved node-keys; only *edge* sub-scores die. The fix is aimed partly at a problem that isn't there, and the real loss (edges) is narrower than billed. |
| 3 | N×M LLM-call explosion vs today's exactly-1 batched resolution call | Cost / latency / scale | **Critical** | Today: 1 adjudication call/attempt (`resolver.py:184`). Claim-alignment is student-claims × candidate-refs × judges. On the Done path (latency-sensitive, in a cross-store txn window) this is a 10–50× cost and latency regression with new infra-failure surface. |
| 4 | Semantic contracts relocate brittleness from aliases to hand-authored prose, at larger scale, with no authoring/validation story | Authoring burden | **Critical** | Every reference node, every problem, needs `canonical_claim`/`required_meaning`/`partial_credit_rules`/`contradictions`. Stale/wrong/missing contracts silently mis-grade. This is strictly MORE authoring than the aliases the doc dismisses as "not enough." |
| 5 | Scoring/aggregation math is left as "open question 4" — the actual grade computation is unspecified | Scoring underspecification | **Critical** | coverage/soundness are currently set/structural, not float-weighted (`coverage.py:40`, `soundness.py:49`). "Combine a 0.6 and 0.9 into an edge score" has no defined function. The design ships everything EXCEPT the grade. |
| 6 | LLM-graded floats destroy determinism + break the 95% patch-coverage contract | Determinism / testing | **High** | Current core is pure + deterministic (`core.py` docstring, `canonical.py`). Graded floats from GPT-4o are non-reproducible run-to-run and cannot be unit-tested to a fixed value; the repo's hard 95% patch-coverage gate has no defined way to test a stochastic float. |
| 7 | Guardrails are mostly aspirational, not enforceable | Guardrails | **High** | "Candidates from current problem" is already true today and did NOT prevent the LLM cross-type bug (Problem 4). "Low-confidence shouldn't drive high-stakes" has no threshold, no mechanism. They read as intentions, not constraints. |
| 8 | "Fixes 1b" claim leans on the SAME LLM judgment that already fails today (Problem 4's cross-type LLM resolution) | Does it fix it? | **High** | 1b (free-text conditions) would be graded by `semantic_contract_judge` — an LLM. The one existing LLM path in resolution already bypasses the type gate and mis-resolves (`resolver.py:182`; findings doc Problem 4). Direction C multiplies reliance on that exact weak component. |
| 9 | Two parallel truth systems (`resolved_key` AND `alignments`) with no conflict-resolution rule | Migration / coexistence | **High** | The doc says keep both but never says who wins when `resolved_key=eq.gdp_deflator` (exact) disagrees with an alignment of 0.6 to a different node. Two sources of truth with no arbiter is a grade-nondeterminism generator. |
| 10 | Ground-up re-architecture of grading dressed as a case-3 bug fix; blast radius is the whole `graph_compare` + `grading` chain | Scope creep | **High** | "Change the core representation from binary resolution to graded evidence" (brainstorm L785) touches resolver, canonical, scores, soundness, coverage, audited_grade, abstention, calibration, persistence. This is not a bug fix. |
| 11 | Problems 3 (over-merge self-loop) and 4 (cross-type LLM) are left untouched or made worse | Does it fix it? | **Medium** | The findings doc's own Impact section says Direction B "does not fix" 3/4; Direction C is B+ and inherits that. Adding MORE LLM judgment (contracts) widens Problem 4's failure surface. |
| 12 | Misconception inflation: "detect wrongness without the bank" replaces a precise structural signal with a fuzzy LLM `contradiction_score` | New failure mode | **Medium** | Today a contradiction is a resolved `misc.*` key — precise, deterministic (`soundness.py:46-49`), and competition-screened. A graded `contradiction_score` float invites both false contradictions (penalizing correct answers) and missed ones. |
| 13 | `numeric_instance_of` / `solved_form_of` credit is a gameable correctness laundering channel | Gaming / over-credit | **Medium** | A student who writes only `realGDP = 543.3 * 0.19` (a bare number with no reasoning) could earn 0.9 "numeric_instance_of" credit — the design's own example (brainstorm L448-457). Plugging in numbers is not understanding; this rewards it. |
| 14 | Confidence-decomposition (correctness/completeness/contradiction/confidence) quadruples the LLM's degrees of freedom to hallucinate | Determinism / over-credit | **Medium** | Four independent floats per claim, each LLM-emitted, none calibrated. More knobs = more surface for the judge to produce a plausible-but-wrong number. |
| 15 | `partial_credit_rules` are natural-language `condition` strings matched by an LLM — unbounded and untestable | Authoring / determinism | **Low→Medium** | e.g. `"Student says the price index adjusts nominal GDP but does not identify it as the GDP deflator" → 0.6` (brainstorm L353-355). Whether a given utterance "matches" that condition is itself an LLM judgment call with no ground truth. |

---

## Detailed findings

### FLAW 1 (Critical) — Direction C disables the only hard quality gate by construction

The system has exactly ONE gate that stops a grade from updating the learner model: the unresolved-rate abstention.

- `abstention.py:31-35`: `"unresolved_rate": 0.35` — strictly the only threshold that sets `abstained=True`.
- `abstention.py:106`: `abstained = unresolved_rate > ABSTENTION_THRESHOLDS["unresolved_rate"]`.
- `abstention.py:59-67`: `unresolved_rate_of` counts every node where `resolution != "resolved"`.
- Docstring (`abstention.py:11-13`): "`abstained=True` is RESERVED for the *no-Layer-3-update* run — only the `unresolved_rate` gate sets it."

Direction C's entire purpose is to make unresolved nodes earn credit ("Keep unresolved claims first-class and evaluate graded semantic alignment," brainstorm L770). If unresolved nodes now produce alignment-based credit, one of two things happens, both bad:

1. The team keeps the gate keyed on `resolution != "resolved"` — then a graph where 60% of nodes are "unresolved but well-aligned" still abstains, and Direction C delivered nothing on its headline case while adding all its cost.
2. The team re-keys the gate to fire on low *alignment* instead — then the circuit-breaker is now governed by the same LLM floats the design introduces, and a confidently-hallucinated set of 0.7 alignments keeps the gate open on an answer that should have abstained.

**Why it bites:** The 40%-edge-loss finding (FINDINGS L30) means many real attempts are riding right at the unresolved-rate boundary. Direction C pushes them under the gate, converting *honest abstention* into *confident wrong grades* — the worst possible outcome for a grader. **No mitigation noted in the brainstorm; "open question 4" doesn't mention the gate at all.**

---

### FLAW 2 (Critical) — The central premise misreads the current system

The brainstorm repeats, as its thesis, that an unresolved node "effectively disappears from structural scoring" (L18) and "Apollo currently grades names, not meaning" (L100), implying that if a node fails resolution the student gets nothing.

That is **false for node coverage and soundness — the two top-line dimensions**:

- **Coverage** is pure set-membership over the S_norm node-key set, computed in `coverage.py:40`: `student_keys = {n.canonical_key for n in student.nodes}` then `covered = ... if k in student_keys`. A node that resolves contributes to coverage regardless of any edge. Coverage is `node_coverage` (`scores.py:69`, `node_coverage=winning_path.score`) and is the top-line `coverage_score` (`core.py:87,98`).
- **Soundness** is structural misconception-key detection (`soundness.py:46-49`): a node counts as a contradiction iff its `canonical_key` starts with `misc.`. Unresolved nodes carry ZERO soundness penalty by explicit design (`soundness.py:12-16`).
- **bisimilarity** = harmonic mean of soundness and coverage (`core.py:93`).

So the three top-line scores are driven by **node** resolution, not edge resolution. What actually dies on an unresolved endpoint is narrower: the *edge* sub-scores (`edge_coverage`, `scoping`, `usage`, `dependency`) via the drop at `canonical.py:275`, and the exact-triple edge match at `core.py:180`. The findings doc is precise about this ("The scorer (step 6) is blameless. All loss is in steps 3 and 5," FINDINGS L303); the brainstorm over-generalizes it into "grades names not meaning."

**Why it bites:** A redesign justified by a misstated problem will over-build. The brainstorm proposes replacing node coverage with "max alignment score" (brainstorm L858) — but node coverage is NOT broken; it already credits any resolved node. Introducing LLM-graded floats into the one dimension that currently works (deterministic set membership) is a pure regression in reliability for no recovered loss.

---

### FLAW 3 (Critical) — N×M LLM-call explosion against a 1-call baseline, on the latency-critical Done path

Verified current cost on the resolution path the brainstorm targets:

- `resolver.py:182-184`: `if remaining and llm_adjudicator is not None: llm_resolved = adjudicate(...); llm_calls = 1`. It is **one** call, batched over ALL remaining nodes (`adjudication.py:90-113` builds a single request with every node and every candidate).
- The full Done shadow chain adds exactly two more 1-shot calls: the transcript audit (`audited_grade.py:181` → `transcript_audit.py:135` `main_chat(...)`) and the constrained diagnostic (`done_grading.py:283` → `diagnostic.py:258`). **Total LLM calls per attempt today: 3, all batched, all single-shot.**

Direction C's proposed pipeline (brainstorm L542-549, L561-569): "evaluate graded claim alignments" + "evaluate graded edge alignments," where each student claim is judged against each plausible reference candidate. The brainstorm's own `ClaimAlignment` examples show one judge call producing one (student_claim, reference_key) pair. With ~15-25 candidates per attempt (`candidates.py:7`) and a typical attempt of a dozen-plus claims, the candidate-narrowing step (guardrail 3, brainstorm L725) still leaves multiple candidates per claim. Even at a conservative 3 candidate refs per claim × 12 claims = **36 judge calls**, plus separate edge-alignment calls. That is a **10×–50× increase** over the current 1 resolution call.

Compounding factors the brainstorm ignores:

- This runs **inside the cross-store transaction window** (`done_grading.py:207-260`), where every added second holds DB state and every added call is a new `ResolutionUnavailableError`/timeout surface (`done_grading.py:84-88` NO-FALLBACK: any infra failure sets `learner_update_pending` and re-raises). 36 calls is 36× the probability of tripping that path per attempt.
- Done is student-facing and latency-sensitive (the student clicked "Done" and is waiting).
- The current design deliberately caps at one call ("ONE `main_chat` call per attempt MAX," `adjudication.py:2`) precisely to bound this.

**Why it bites:** Direct API-cost and latency regression at the most cost-sensitive point in the product, plus a multiplied infra-failure rate that pushes attempts into the dead-letter/retry janitor. **Open question 6** ("how do we prevent over-crediting") and **guardrail 3** ("LLM judges only after candidate narrowing") acknowledge the cost exists but quantify nothing.

---

### FLAW 4 (Critical) — Semantic contracts relocate brittleness to hand-authored prose at greater scale

The brainstorm dismisses aliases as "still basically a lookup strategy" (L245) and proposes semantic contracts instead (brainstorm L336-404). But examine what a contract requires per reference node:

- `canonical_claim` (one string)
- `required_meaning` (a list of natural-language meaning statements)
- `partial_credit_rules` (a list of `{condition: natural-language, score: float}`)
- `contradictions` (a list of `{condition: natural-language, score: float}`)

This is authored per reference node, per problem. The current candidate builder authors **nothing** for non-equation nodes — `candidates.py:101` sets `aliases=()` and uses the existing `display_name`. So the brainstorm replaces "author aliases (which we don't even do today)" with "author four structured prose fields per node, with calibrated float scores, for every problem." That is **strictly more** authoring burden, not less.

Failure modes the brainstorm does not address (open question 5 merely asks "hand-authored, auto-generated, or curated?" and leaves it unanswered):

- **Wrong contract** → systematically mis-grades every student on that node, invisibly (a bad `partial_credit_rule` of 0.6 over-credits everyone hitting it).
- **Stale contract** → the reference solution evolves, the contract doesn't; now the contract contradicts the reference graph it's supposed to describe. (The repo already has a drift-prevention contract for docs *because* this exact failure recurs — CLAUDE.md "Stale docs are worse than no docs.")
- **Auto-generated contracts** → generated BY an LLM FROM the reference node, then graded BY an LLM against the student. The grade is now LLM-vs-LLM with the reference node as a thin seed: a closed loop of model judgment with no deterministic anchor.

**Why it bites:** The findings doc's diagnosis is "non-equation resolution is structurally weak because there are no aliases" (FINDINGS L176-186). The cheap, testable, deterministic fix is to add aliases (a `tuple[str,...]` already supported at `candidates.py:70` and consumed by deterministic tiers `tiers.py:210,269`). Direction C skips that and jumps to hand-authored prose contracts graded by an LLM — moving from a brittleness that's deterministic and fixable to one that's stochastic and unauditable.

---

### FLAW 5 (Critical) — The grade computation itself is unspecified (open question 4)

The design specifies a rich intermediate object (`ClaimAlignment`, brainstorm L296-311) but **open question 4** — "How should claim scores aggregate into existing dimensions like coverage, usage, soundness, and bisimilarity?" (brainstorm L924) — is the actual grade math, and it is left open.

This is not a peripheral gap. The current dimensions are computed by specific, simple, deterministic functions:

- coverage = covered/total set membership per path, max over paths (`coverage.py:35-57`, `core.py:87`).
- edge sub-scores = matched/total over reference edges, with a single documented `INFERRED_EDGE_WEIGHT = 0.5` knob (`scores.py:44,79-101`).
- soundness = `1 - min(1, n_contradictions * 0.5)` (`soundness.py:52-65`).
- bisimilarity = harmonic mean (`core.py:93`).

Every one of these is integer-ratio or fixed-constant arithmetic. Direction C must define how a set of graded floats {0.6, 0.9, 0.4, ...} collapses into each of these. The brainstorm's only gesture is "node coverage for eq.X = max alignment score from student claims" (L858) and "edge usage = score of best aligned student USES claim" (L859). "Max alignment" means:

- A single confident-but-wrong 0.9 alignment sets full node coverage. There is no consensus requirement, no penalty for the 5 other low alignments on the same claim.
- "Best aligned" is `argmax` over LLM floats — maximally optimistic, maximally gameable.

And **open question 9** ("multiple alignments per student claim, or force a best alignment?") means double-counting is unresolved: one verbose student claim could align to 3 reference nodes at 0.7 each and cover all three, inflating coverage from one sentence.

**Why it bites:** A grading redesign that does not specify how it computes the grade is not implementable as written. Whoever picks it up invents the aggregation, and the arbitrary weights they choose ARE the grader. The hard part was deferred to an open question.

---

### FLAW 6 (High) — LLM-graded floats break determinism and the 95% patch-coverage contract

The current grading core is explicitly pure and deterministic:

- `core.py:16-18`: "Pure + deterministic: identical inputs yield an equal `GradeResult`."
- `canonical.py:24`: "calls neither Neo4j nor any LLM."
- `resolver.py:16-17`: "Pure + synchronous + deterministic given a deterministic `llm_adjudicator`: re-running on the same `(student_graph, candidates)` yields the same result."

The single existing LLM touch-point in resolution is deliberately isolated and stubbed in every test (`adjudication.py:11-14`, `resolver.py:139-142` defaults `llm_adjudicator=None` "CI-safe"). The design's whole point is to inject graded LLM floats into the SCORE (not just identity): correctness/completeness/contradiction/confidence per claim (brainstorm L614-626), then aggregate them into coverage/usage/soundness.

Two consequences:

1. **Run-to-run instability.** GPT-4o at temperature 0 is not bit-reproducible across calls/model updates. The same student answer can score 0.62 today, 0.68 next week — a grade that changes when nothing the student did changed. For a grade students see, that is indefensible.
2. **The 95% patch-coverage gate has no defined satisfaction path.** CLAUDE.md: "Patch coverage of any code added or edited must never be less than 95%." You cannot unit-assert `score == 0.6` on a real LLM output, and the current tests get determinism precisely by stubbing the one LLM call. With graded floats woven through the score, what does the stub return, and what does a passing assertion even mean? The brainstorm's "regression tests" (L879-912) are all phrased as "should align to X as numeric_instance_of" — i.e., they assert the *relation label*, never a numeric score band, quietly conceding the floats aren't directly testable.

**Why it bites:** Either you stub the judge (and your tests prove nothing about real grading behavior) or you call it live (and CI is non-deterministic and expensive). The current architecture sidesteps this by keeping scoring pure; Direction C reintroduces exactly the problem the WU-4A2 split was built to avoid.

---

### FLAW 7 (High) — Guardrails are largely aspirational

Reading the Guardrails section (brainstorm L714-747) against what's enforceable:

- **G1 "Candidates must come from the current problem"** — already true today: the candidate set is the closed per-problem set (`candidates.py:7`, `build_candidate_set` = this problem's refs + course misconceptions). It did NOT prevent the LLM cross-type bug (Problem 4, FINDINGS L219-244). So this guardrail is both already-satisfied and already-insufficient.
- **G4 "Type compatibility strict for identity resolution"** — `type_compatible` IS strict (`structural.py:42-46`, "No cross-type resolution, ever"). But the findings doc proves the LLM adjudication path bypasses it (FINDINGS L233-236; `resolver.py:182` `adjudicate` does not re-apply `type_compatible`). The guardrail exists today and is already violated by the LLM path Direction C leans on harder.
- **G8 "Low-confidence alignments should not drive high-stakes scores without fallback/review"** — no threshold, no definition of "high-stakes," no mechanism. Compare the current system: it has a *named, numeric* gate (`abstention.py:31`, `unresolved_rate > 0.35`) and method-capped confidences (`candidates.py:35-42`). G8 is a wish, not a control.
- **G2 "Equation alignments should use deterministic symbolic/numeric checks whenever possible"** — fine, but this is Direction A (improve the symbolic tier), which already exists (`tiers.py:183-202`) and which the findings doc says only fixes 1a. G2 doesn't help the free-text 1b case the design claims as its differentiator.

**Why it bites:** The guardrails that would actually constrain over-crediting (G8) are undefined; the ones that are defined (G1, G4) are already present AND already demonstrably bypassed by the LLM. The section creates an impression of safety the design doesn't deliver.

---

### FLAW 8 (High) — "Fixes 1b" relies on the same LLM judgment that already fails

1b is the free-text bucket: conditions/simplifications/definitions with no aliases (FINDINGS L160-186). Direction C's fix is the `semantic_contract_judge` (brainstorm L328 `"method": "semantic_contract_judge"`; Step 4, L836-849: classify entails/partially_entails/contradicts/unrelated). That is an LLM judging free text against a prose contract.

But the existing single LLM resolution call is precisely the component that already misbehaves on these nodes:

- Problem 4 (FINDINGS L219-244): a Definition node cross-type-resolved to a Simplification candidate **through the LLM path**, bypassing the hard type gate. Code: `resolver.py:182` calls `adjudicate`, and `adjudication.py:90-121` never re-checks `type_compatible`.
- The findings doc's own conclusion (FINDINGS L186): "the handoff's equation-only symbolic fix cannot help [1b]" — true, but Direction C's answer is *more* of the same LLM judgment, just dressed in contract scaffolding.

So Direction C "fixes" 1b by routing it to a more elaborate version of the exact mechanism that today produces the cross-type and over-merge errors. It does not add a deterministic channel for free-text correctness; it adds prose and floats around the existing weak judge.

**Why it bites:** The cheap, deterministic, already-supported alternative — author `aliases` for the legitimate course conditions/simplifications so the deterministic `exact`/`alias`/`fuzzy` tiers (`tiers.py:100,210,269`) can fire — is dismissed in the "Why aliases are not enough" section (brainstorm L240-277) in favor of LLM contracts. The design rejects the reliable fix for 1b and doubles down on the unreliable one.

---

### FLAW 9 (High) — Two parallel truth systems with no arbiter

The brainstorm keeps `resolved_key` AND adds `alignments` (brainstorm L575-605: "Do not remove `resolved_key` ... add a softer layer"). It never defines precedence when they disagree.

Concrete disagreement: a student writes `deflator = (nomGDP/realGDP)*100` verbatim. Today that exact-matches (`tiers.py:113`) → `resolved_key = eq.gdp_deflator`, confidence 1.0 (`candidates.py:36`). Under Direction C the claim-alignment layer ALSO runs and might emit an alignment to a *different* node at 0.7 (LLM judges are not consistent with the symbolic matcher). Now:

- Does coverage use the exact `resolved_key` (deterministic, correct) or the alignment `max` (brainstorm L858)?
- If a node has `resolved_key = eq.A` but its highest alignment is to `eq.B`, which key gets coverage credit? Which edge endpoint does it normalize to (`canonical.py:273-274` currently uses `resolved_key`)?

The merge step `build_student_canonical` (`canonical.py:215`) is built entirely around `resolved_key` grouping (`canonical.py:230-241`). Bolting an `alignments` layer beside it without redefining the merge means two code paths can assign a node two different canonical identities.

**Why it bites:** Two sources of truth with no documented winner is a grade-nondeterminism bug generator. Step 6's calibration ("old score vs new score diff," brainstorm L862-874) has **no defined acceptance bar** — "Then calibrate thresholds" (L874) is the entire spec. There is no stated max divergence, no pass/fail, nothing to gate promotion.

---

### FLAW 10 (High) — A ground-up grading re-architecture mislabeled as a bug fix

The brainstorm is explicit: "We are changing the core representation from binary resolution to graded evidence" (L785) and "parse → graded claim alignment → graded edge alignment → graph-level scoring" (L970) replacing "parse → binary resolve → exact graph compare."

Map the blast radius against the code:

- `resolution/resolver.py` — new alignment producer alongside/instead of `_content_match`.
- `graph_compare/canonical.py` — `build_student_canonical` (`canonical.py:215`) must stop dropping edges on unresolved endpoints (`canonical.py:275`) and consume alignments.
- `graph_compare/scores.py` — every sub-score (`scores.py:61-156`) re-derived from floats.
- `graph_compare/coverage.py`, `soundness.py` — set/structural logic replaced by float aggregation.
- `graph_compare/core.py` — `grade_attempt` (`core.py:78`) reshaped; `_edge_findings` (`core.py:174`) currently diagnostic-only must become scoring.
- `grading/abstention.py` — the gate (FLAW 1) must be re-keyed.
- `grading/audited_grade.py`, `calibration.py`, `persistence.py` — the `AuditedGrade` and `apollo_graph_comparison_runs` columns (`core.py:53-75` are named 1:1 to DB columns) need new fields → a **migration** (currently at 028 per the architecture doc).

That is essentially the entire `graph_compare` + half of `grading`, plus a schema change, plus new LLM infra. The trigger was case-3: one derived-equation form fails to resolve (FINDINGS L43-48). The findings doc itself frames the real problem as narrow: "make the resolution→matching boundary tolerant" (FINDINGS L319).

**Why it bites:** Scope. The 95%-patch-coverage contract on this much new stochastic code is an enormous test-writing burden, the migration touches the prod-numbered sequence, and the "minimal practical implementation path" (brainstorm L789-874) is six steps each of which is itself a project (Step 3 alone — `exact_symbolic`/`symbolic_equivalent`/`solved_form_of`/`numeric_instance_of`/`final_value_match`/`known_error_pattern`, L823-834 — is a multi-week symbolic-algebra effort). This will not land as a fix; it will land as a quarter-long rewrite with a half-specified scoring layer.

---

### FLAW 11 (Medium) — Problems 3 and 4 are untouched or worsened

The findings doc's own assessment (FINDINGS L308-319): Direction B "still does not, by itself, fix over-merge (Problem 3) or the LLM type gate (Problem 4); those are separate resolver fixes." Direction C is explicitly "B+" (brainstorm L782). So:

- **Problem 3 (over-merge → PRECEDES self-loop,** FINDINGS L190-216): caused by many-to-one assignment (`assignment.py:50-51`) + merge-by-key (`canonical.py:240-241`). Direction C changes the scoring layer, not the assignment/merge layer. The self-loop that bypasses the ontology guard (`edges.py:73` runs at construction, not post-merge) survives unchanged.
- **Problem 4 (cross-type LLM,** FINDINGS L219-244): Direction C ADDS LLM judgment (semantic-contract judge), so the surface where an LLM produces ontology-violating or cross-type credit grows. Guardrail 6 ("cross-form but not ontology-breaking," brainstorm L731) is the same unenforced wish as G4.

**Why it bites:** The design claims to be the holistic answer ("fix case-3 more naturally, improve non-equation resolution, make misconception detection less brittle," brainstorm L980) but leaves two of the five diagnosed defects in place and enlarges the failure surface of one.

---

### FLAW 12 (Medium) — Misconception detection trades a precise structural signal for a fuzzy float

Current misconception/contradiction detection is deterministic and competition-screened:

- A contradiction is a resolved node whose key is `misc.*` (`soundness.py:41-49`).
- Misconceptions carry `trigger_phrases` as aliases and **compete** against reference nodes on raw lexical proximity, with a polarity screen (`resolver.py:104-126`, `competition.apply_misconception_competition`, `polarity_screen`).
- Misconceptions are always in the candidate set so they always compete (`candidates.py:146-150`).

The brainstorm proposes wrongness be "detected regardless of whether a named misconception exists" via graded alignment (brainstorm L656-695), with a `contradiction_score` float (brainstorm L304, L621). This:

- Introduces **false contradictions**: an LLM emitting `contradiction_score: 0.6` on a correct-but-oddly-phrased claim now penalizes soundness on a right answer.
- Introduces **missed contradictions**: the deterministic `misc.*` chokepoint (which fires reliably once a misconception resolves) is replaced/augmented by a judge that can score a real misconception's contradiction at 0.3 and let it pass.

This is acknowledged nowhere; open question 8 ("how should known misconceptions interact with generic wrong-part detection?") leaves the interaction undefined.

**Why it bites:** Soundness directly feeds bisimilarity (`core.py:93`) and the misconception-confidence abstention gate (`abstention.py:118-122`). Floating the contradiction signal destabilizes a deterministic, conservatively-tuned penalty (`soundness.py:38`, unit 0.5) that the system relies on to NOT over-penalize.

---

### FLAW 13 (Medium) — `numeric_instance_of` is correctness laundering

The design's flagship example credits a bare numeric form: student writes `growth = ((10739.0 - 2859.5) / 2859.5) * 100`, gets `relation = numeric_instance_of, correctness_score = 0.90` (brainstorm L448-457). And `realGDP = 543.3 * 0.19` is cited in the findings doc as a real unresolved form (FINDINGS L140).

A student who simply types the arithmetic with the correct numbers — copied, guessed, or back-solved from an answer key — would earn ~0.9 correctness for the underlying equation node. Plugging numbers in is not evidence of understanding the *relationship*; the relationship is exactly what Apollo grades (it's a teaching system). The current symbolic tier deliberately does NOT credit this: `_symbolic_equiv` is sign-exact structural equivalence (`tiers.py:160-180`), and a solved/numeric form is correctly judged "not literally that equation" (FINDINGS L142-148) — that's a *feature* for a grader, not only a bug.

**Why it bites:** This is the precise false-positive the task warns about. For a grading system, crediting "the student produced the right number" as "the student understands the equation" is a correctness leak that students will find and exploit (paste the computation, skip the reasoning).

---

### FLAW 14 (Medium) — Four uncalibrated LLM floats per claim multiply hallucination surface

The design mandates correctness/completeness/contradiction/confidence per claim (brainstorm L614-626, schema L300-310). None has a calibration procedure (open question 7 asks for thresholds and leaves them open). Each is an independent number the judge invents. Four floats × N claims × M candidates is a large space of model-produced numbers feeding a grade, with no ground-truth anchor and no inter-rater check.

**Why it bites:** More degrees of freedom = more ways to be confidently wrong. The current system caps confidence by *method* (`candidates.py:35-42`: exact 1.0 → fuzzy 0.80 → llm 0.75 → unresolved 0.0), a deterministic ladder. Replacing that with free LLM floats removes the one calibration the system has.

---

### FLAW 15 (Low→Medium) — `partial_credit_rules` conditions are unbounded NL matched by an LLM

The contract's partial-credit and contradiction rules are natural-language `condition` strings paired with scores (brainstorm L352-372). e.g. `"Student says the price index adjusts nominal GDP but does not identify it as the GDP deflator" → 0.6`. Whether an arbitrary student utterance "satisfies" that condition is itself an LLM classification with no deterministic test. Authors must anticipate the space of student phrasings as enumerated rules — an open-ended, never-complete list — and every rule is a new prose-vs-prose judgment.

**Why it bites:** It's the alias-brittleness problem (anticipate the phrasings) plus LLM-nondeterminism (judge the match) plus authoring burden (write+calibrate each rule), combined. It is the worst of all three layers the design was supposed to improve on.

---

## Fatal-if-unaddressed vs manageable

### Fatal if unaddressed (block the design until answered)

- **FLAW 1 — disables the abstention gate by construction.** The grader's only circuit-breaker. If Direction C credits unresolved nodes, the gate either does nothing or is governed by LLM floats. No design that silently turns abstention into a confident grade can ship to students.
- **FLAW 2 — premise misreads the system.** The justification ("grades names not meaning") is materially wrong for the two top-line dimensions. The design must be re-scoped to the *edge* loss it actually addresses before any build.
- **FLAW 3 — N×M cost/latency on the Done path.** A 10–50× LLM-call regression inside the cross-store txn window on the student-facing path. Must be quantified and bounded before commitment.
- **FLAW 5 — the grade math is an open question.** The aggregation from floats into coverage/usage/soundness/bisimilarity is undefined. The design is not implementable as written; the hard part is deferred.
- **FLAW 6 — determinism + 95% patch-coverage contract.** No defined way to test LLM-graded floats under the repo's hard coverage gate; grades become run-to-run unstable.

### Manageable (real, but fixable within the design if the fatals are resolved)

- FLAW 4 (contract authoring) — could be bounded by starting with deterministic `aliases` (already supported) and treating contracts as opt-in for a handful of high-value nodes.
- FLAW 7 (aspirational guardrails) — fixable by giving G8 a numeric threshold and re-applying `type_compatible` on the LLM path.
- FLAW 9 (two truth systems) — fixable by a written precedence rule (`resolved_key` always wins; alignment only fills where `resolved_key is None`) and a numeric Step-6 acceptance bar.
- FLAW 11 (Problems 3/4) — orthogonal resolver fixes; can be done independently and should be, regardless of Direction C.
- FLAW 12, 13, 14, 15 — all mitigable by keeping correctness floats OUT of soundness, refusing standalone numeric-instance credit, capping alignment confidence by method, and forbidding free-text partial-credit conditions in favor of structured ones. But each mitigation chips away at the design's stated value.

---

## Net assessment — the single most dangerous assumption

**The single most dangerous assumption is that "crediting more of what currently doesn't resolve" is safe for a grader — that recovering the 40% of dropped edges is upside with no symmetric downside.**

It is not. The current pipeline's conservatism (drop the edge, abstain over 35% unresolved, sign-exact symbolic only, deterministic `misc.*` contradictions, method-capped confidence) is what keeps it from crediting wrong or vague answers. Every one of those conservative choices is something Direction C loosens: edges survive on graded alignments, the abstention gate is undermined (FLAW 1), numeric/solved forms get credit (FLAW 13), contradictions become floats (FLAW 12), and confidence becomes free LLM output (FLAW 14). The design optimizes hard for recall (catch the partially-right student) with no quantified control on precision (don't credit the confidently-wrong or the gamer) — in a system where the task itself states false positives are worse than false negatives.

Worse, it does this by **deepening reliance on the exact component that already fails** — the single LLM judgment call that today bypasses the type gate and produces the cross-type/over-merge defects (FLAW 8, Problem 4) — while **rejecting the cheap deterministic fix** (author `aliases`, already supported by the code at `candidates.py:70`/`tiers.py:210`) that would address the real 1b loss without any of this risk. The honest, in-scope fix is two narrow, testable, deterministic changes: (1) add `aliases` to non-equation reference candidates (kills most of 1b), and (2) add one-anchor edge recovery that compares an unresolved endpoint only against the edges the *resolved* anchor is expected to use (Step 2 of the brainstorm's own path, brainstorm L807-820 — the one genuinely tight idea in the document). Everything else in Direction C is a stochastic re-architecture of a working deterministic grader, justified by a misread of what that grader actually does.
