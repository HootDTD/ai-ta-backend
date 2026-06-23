# Brainstorming Note — Inverting Apollo’s Graph-Grading Order: From Binary Resolution to Graded Claim Evaluation

## Core intuition

The current Apollo graph-grading pipeline appears to treat grading as a form of entity recognition:

> “Can this student node be resolved to exactly one known reference node?”

Only after that does the system attempt to score correctness.

This creates a binary bottleneck:

```text
student node → resolved_key OR unresolved
```

If the node resolves, it can receive credit.
If it does not resolve, it effectively disappears from structural scoring, especially when unresolved endpoints cause edges to be dropped.

This is the heart of the case-3 problem, but the deeper issue is broader than case-3. The system is currently asking the wrong first question.

Instead of asking first:

```text
Which canonical node is this?
```

we should ask:

```text
What is the student trying to say?
How much of that claim is correct?
How much is incomplete?
How much is wrong?
Which reference claims does it support, partially support, or contradict?
```

That means the grading pipeline should move from **binary node resolution** to **graded claim alignment**.

---

## Current failure mode

The present pipeline roughly behaves like this:

```text
1. Parse student answer into nodes and edges.
2. Resolve each student node in isolation to a reference entity.
3. Build canonical student graph.
4. Drop edges if either endpoint is unresolved.
5. Score exact node/edge overlap against the reference graph.
```

The problem is that step 2 is too brittle and too early.

A student can make a meaningful, partially correct, or contextually valid claim without producing a node that cleanly maps to one pre-authored reference entity.

Examples:

```text
"The price index tells you how to adjust nominal GDP."
```

This may be semantically close to a simplification like:

```text
The given price index should be treated as the GDP deflator.
```

But it may not exactly resolve.

Another example:

```text
realGDP = nomGDP / (PI / 100)
```

This is a solved form of the GDP deflator relationship, but it is not symbolically identical to the base equation:

```text
deflator = (nomGDP / realGDP) * 100
```

A human grader sees the relationship. The current resolver may not.

So the system ends up doing this:

```text
student said something meaningful
→ node fails binary resolution
→ edge touching it is dropped
→ scorer never sees the structural claim
→ usage/edge coverage collapse
```

The student is not necessarily wrong. The system just failed to name the claim.

---

## The deeper problem: Apollo currently grades names, not meaning

The current system is too dependent on canonical identity.

It assumes:

```text
If I can name the student node, I can grade it.
If I cannot name the student node, I cannot grade it.
```

But that is not how human grading works.

Human grading does not require the student’s statement to be perfectly nameable as a pre-existing node. A human grader evaluates the **meaning** of the statement.

For example, a student might say:

```text
"You use the old GDP as the base and compare the change to that."
```

This is not a clean equation node. But it strongly supports the concept:

```text
growth = ((g2 - g1) / g1) * 100
```

The student may not have stated the exact formula, but they captured a crucial part of the reasoning: the old value is the denominator.

The system should represent that as partial or strong evidence, not unresolved/no credit.

---

## Proposed reframing

The grading unit should not be “node identity.”

The grading unit should be an **atomic student claim**.

A claim is something like:

```text
Student claims that imports are subtracted from exports to compute net exports.
Student claims that real GDP adjusts nominal GDP for price level.
Student claims that the growth formula divides by the old value.
Student claims that the price index can be used as the GDP deflator.
Student claims that transfers count as GDP.
```

Each claim can then be evaluated as:

```text
correct
mostly correct
partially correct
incomplete
incorrect
contradictory
irrelevant
underspecified
```

This creates a richer grading model:

```text
student statement
→ candidate reference claims
→ graded alignment
→ graph-level score
```

instead of:

```text
student node
→ resolved/unresolved
→ exact graph overlap
```

---

## Key architectural insight: invert the order

The current order is:

```text
Resolve identity first.
Then grade.
```

The proposed order is:

```text
Evaluate semantic alignment first.
Then decide how much canonical graph credit it supports.
```

This is the inversion.

In the current system, the resolver must decide whether a node “is” a reference node before any scoring can happen.

In the proposed system, the evaluator asks how much the student claim supports each plausible reference claim, even if the student node cannot be perfectly named.

So instead of:

```text
resolved_key = eq.growth_rate
```

or:

```text
unresolved
```

we would produce something like:

```json
{
  "student_claim": "growth = (10739.0 - 2859.5) / 2859.5 * 100",
  "alignments": [
    {
      "reference_key": "eq.growth_rate",
      "relation": "numeric_instance_of",
      "correctness_score": 0.90,
      "completeness_score": 0.80,
      "confidence": 0.92,
      "missing_parts": ["does not explicitly name variables g1 and g2"],
      "wrong_parts": [],
      "basis": "Student substituted the correct values into the growth-rate equation."
    }
  ]
}
```

This is much more informative than binary resolution.

---

## Why aliases are not enough

Adding aliases to reference nodes may help, but it is not the fundamental solution.

Aliases are still basically a lookup strategy:

```text
Does the student’s wording match one of these acceptable strings?
```

This increases recall, but it still treats the problem as naming.

The real issue is not merely that we lack enough aliases. The real issue is that the system needs to judge semantic correctness by degree.

For example:

```text
"The price index adjusts nominal GDP."
```

This might not exactly equal:

```text
"The price index is the GDP deflator."
```

But it partially supports the intended simplification.

An alias system may either miss it or over-credit it. A graded semantic evaluator can say:

```text
This captures that the price index is used for adjustment, but does not explicitly state that the price index is being treated as the GDP deflator.
Credit: 0.6
```

That is the kind of judgment we actually want.

Aliases can remain useful as retrieval hints, but they should not be the core correctness mechanism.

---

## Proposed intermediate object: ClaimAlignment

Introduce a first-class intermediate object between parsing and graph scoring:

```text
ClaimAlignment
```

A `ClaimAlignment` says:

```text
This student claim provides some degree of evidence for this reference claim.
```

Possible structure:

```json
{
  "student_claim_id": "stu_123",
  "student_surface": "real GDP is nominal GDP divided by the price index over 100",
  "reference_key": "eq.gdp_deflator",
  "relation": "solved_form_of",
  "correctness_score": 0.95,
  "completeness_score": 0.85,
  "contradiction_score": 0.0,
  "confidence": 0.90,
  "missing_parts": [],
  "wrong_parts": [],
  "method": "symbolic_solved_form_checker",
  "basis": "Equivalent solved form of the GDP deflator equation using PI as deflator."
}
```

For free text:

```json
{
  "student_claim_id": "stu_456",
  "student_surface": "The price index tells us how to adjust nominal GDP.",
  "reference_key": "simp.deflator_is_price_index",
  "relation": "semantically_supports",
  "correctness_score": 0.65,
  "completeness_score": 0.60,
  "contradiction_score": 0.0,
  "confidence": 0.82,
  "missing_parts": ["Does not explicitly state that the price index is the GDP deflator."],
  "wrong_parts": [],
  "method": "semantic_contract_judge",
  "basis": "Student understands that the price index is used to adjust nominal GDP, but the equivalence to GDP deflator is implicit."
}
```

This lets the system represent partial understanding.

---

## Semantic contracts instead of just aliases

Each reference node should not merely have aliases. It should have a semantic contract.

A semantic contract defines what it means to understand that node.

Example for a simplification:

```json
{
  "reference_key": "simp.deflator_is_price_index",
  "canonical_claim": "The given price index should be treated as the GDP deflator for this problem.",
  "required_meaning": [
    "The price index and GDP deflator refer to the same usable quantity in this calculation.",
    "This quantity is used to convert nominal GDP into real GDP."
  ],
  "partial_credit_rules": [
    {
      "condition": "Student says the price index adjusts nominal GDP but does not identify it as the GDP deflator.",
      "score": 0.6
    },
    {
      "condition": "Student names GDP deflator but does not explain its use.",
      "score": 0.5
    }
  ],
  "contradictions": [
    {
      "condition": "Student says nominal and real GDP are basically the same.",
      "score": 0.0
    },
    {
      "condition": "Student multiplies by the price index when the correct operation is division by the deflator ratio.",
      "score": 0.2
    }
  ]
}
```

For an equation, the contract can be more deterministic:

```json
{
  "reference_key": "eq.growth_rate",
  "canonical_equation": "growth = ((g2 - g1) / g1) * 100",
  "acceptable_forms": [
    "symbolic_equivalent",
    "solved_for_any_variable",
    "numeric_substitution_using_problem_givens",
    "final_value_with_tolerance"
  ],
  "partial_error_patterns": [
    {
      "error": "uses new value as denominator",
      "score": 0.4
    },
    {
      "error": "forgets multiply by 100",
      "score": 0.7
    },
    {
      "error": "uses nominal GDP instead of real GDP",
      "score": 0.3
    }
  ]
}
```

This moves the system from string matching to meaning evaluation.

---

## Case-3 under the new model

In case-3, the student gives a derived, solved, or computed form of an equation.

Current model:

```text
student equation does not equal canonical equation
→ unresolved
→ edge dropped
→ no usage credit
```

New model:

```text
student equation is compared against equation semantic contract
→ recognized as solved_form_of or numeric_instance_of
→ receives high correctness score
→ supports the relevant reference node and edge
```

Example:

```text
Student:
realGDP = nomGDP / (PI / 100)

Reference:
deflator = (nomGDP / realGDP) * 100

Alignment:
relation = solved_form_of
correctness_score = 0.95
basis = "Solving reference equation for realGDP gives the student form under PI = deflator."
```

Another example:

```text
Student:
growth = ((10739.0 - 2859.5) / 2859.5) * 100

Reference:
growth = ((g2 - g1) / g1) * 100

Alignment:
relation = numeric_instance_of
correctness_score = 0.90
basis = "Student substituted the correct new and old real GDP values into the growth-rate formula."
```

The student did not literally state the canonical symbolic equation, but they demonstrated the right reasoning.

So the system should not force this to resolve as the canonical node. It should store the more precise relation:

```text
numeric_instance_of eq.growth_rate
```

or:

```text
solved_form_of eq.gdp_deflator
```

This preserves meaning and auditability.

---

## Edge scoring under the new model

Current edge scoring is binary:

```text
Does this exact edge exist between resolved canonical keys?
```

New edge scoring should use aligned claims.

For a student edge:

```text
ProcedureStep USES raw_equation
```

we should ask:

```text
Does the procedure step align to a reference procedure?
Does the raw equation align to the equation that procedure should use?
How strong are those alignments?
```

Example:

```json
{
  "student_edge": "stu_proc_1 USES stu_eq_7",
  "reference_edge": "proc.compute_growth USES eq.growth_rate",
  "edge_alignment_score": 0.85,
  "basis": "Procedure step aligns to compute_growth, and equation is a numeric instance of growth_rate.",
  "endpoint_alignments": {
    "from": {
      "reference_key": "proc.compute_growth",
      "score": 0.90
    },
    "to": {
      "reference_key": "eq.growth_rate",
      "score": 0.90
    }
  }
}
```

This means a structural claim can get credit even if one endpoint never achieved strict binary resolution.

The system can still prefer exact matches. But it should have a graded fallback.

---

## Proposed pipeline

Replace:

```text
parse
→ binary resolve nodes
→ drop unresolved edges
→ exact graph score
```

with:

```text
parse
→ extract student claims
→ retrieve plausible reference candidates
→ evaluate graded claim alignments
→ evaluate graded edge alignments
→ compute graph score from weighted evidence
```

More concretely:

```text
1. Parse layer
   Capture what the student literally said as nodes, edges, and raw claims.

2. Candidate layer
   Retrieve plausible reference claims for each student claim.
   Aliases can help here, but are not the source of truth.

3. Semantic correctness layer
   Evaluate how much the student claim supports, partially supports, or contradicts each candidate reference claim.

4. Structural alignment layer
   Evaluate whether student edges express correct relationships between aligned claims.

5. Graph scoring layer
   Aggregate node-level and edge-level evidence into coverage, usage, soundness, dependency, etc.
```

This separates “finding candidates” from “judging correctness.”

---

## What happens to `resolved_key`?

Do not remove `resolved_key`. Keep it as a strict, high-confidence identity label.

But add a softer layer:

```text
resolved_key: exact or high-confidence identity
alignments: graded evidence relationships
```

So a node can be unresolved in the strict sense but still useful for grading:

```json
{
  "student_node_id": "stu_eq_7",
  "resolved_key": null,
  "alignments": [
    {
      "reference_key": "eq.growth_rate",
      "relation": "numeric_instance_of",
      "score": 0.90
    }
  ]
}
```

This avoids lying to the graph.

The system should not pretend the student literally said the canonical equation if they actually said a numeric instance. It should represent the relationship accurately.

---

## Correctness should be decomposed

A single score is not enough.

For each claim, track at least:

```text
correctness_score:
How true is the claim?

completeness_score:
How much of the expected idea is present?

contradiction_score:
Does the claim actively conflict with the reference?

confidence:
How certain is the system about this judgment?
```

Examples:

```text
Student: "GDP includes consumption, government spending, and net exports."
Reference: GDP = C + I + G + NX
Correctness: 0.9
Completeness: 0.75
Missing: investment

Student: "Imports increase net exports."
Reference: NX = X - M
Correctness: 0.1
Contradiction: high
Wrong part: sign of imports

Student: "Use the old GDP as the denominator."
Reference: growth = ((g2 - g1) / g1) * 100
Correctness: 0.9
Completeness: 0.5
Missing: subtraction and multiply by 100
```

This gives a much more human-like grading signal.

---

## Misconceptions under this model

Misconceptions should become one output of incorrect claim alignment, not the only way to recognize wrongness.

Current issue:

```text
If misconception is in bank → soundness penalty.
If misconception is not in bank → may look sound.
```

New model:

```text
Student claim is semantically evaluated against the reference.
If wrong, the wrongness is detected regardless of whether a named misconception exists.
If the error matches a known misconception, attach that misconception label.
If the error recurs across many students, propose a new candidate misconception.
```

So:

```text
wrongness detection
≠
misconception-bank lookup
```

The bank is useful for diagnosis and tutoring, but not required to know that something is wrong.

Example:

```json
{
  "student_claim": "Imports add to net exports because people buy them.",
  "reference_key": "eq.net_exports",
  "correctness_score": 0.15,
  "wrong_parts": ["imports are treated as positive instead of negative"],
  "known_misconception": "misc.imports_add_to_net_exports",
  "candidate_misconception": null
}
```

If no known misconception exists:

```json
{
  "student_claim": "Imports add to net exports because people buy them.",
  "reference_key": "eq.net_exports",
  "correctness_score": 0.15,
  "wrong_parts": ["imports are treated as positive instead of negative"],
  "known_misconception": null,
  "candidate_misconception": "proposed.imports_positive_in_nx"
}
```

This is more robust.

---

## Guardrails

This should not become an unconstrained LLM vibes system.

Important guardrails:

```text
1. Candidate reference claims must come from the current problem/reference graph, not the whole course by default.

2. Equation alignments should use deterministic symbolic/numeric checks whenever possible.

3. LLM semantic judges should operate only after candidate narrowing.

4. Type compatibility should remain strict for identity resolution.

5. A ProcedureStep should not become an Equation.

6. Claim alignment can be cross-form, but not ontology-breaking.

7. Every alignment must provide:
   - relation type
   - score
   - basis
   - missing parts
   - wrong parts
   - confidence
   - method

8. Low-confidence alignments should not drive high-stakes scores without fallback/review.

9. Exact canonical graph matching should remain as a high-confidence path.

10. Tolerant/semantic matching should be auditable and separately logged.
```

---

## Relationship to Direction A vs Direction B

Direction A says:

```text
Improve resolver so derived equation forms resolve to the governing entity.
```

This helps case-3 but still keeps the architecture centered on node identity.

Direction B says:

```text
Keep unresolved nodes first-class and match edges more tolerantly.
```

This is closer to the better direction, but the deeper version is:

```text
Keep unresolved claims first-class and evaluate graded semantic alignment before graph scoring.
```

So the proposed direction is something like:

```text
Direction C: Graded Claim Alignment
```

or:

```text
Direction B+, where unresolved nodes survive and are scored through semantic claim alignment.
```

The key difference is that we are not merely trying to recover dropped edges. We are changing the core representation from binary resolution to graded evidence.

---

## Minimal practical implementation path

This does not need to be rebuilt all at once.

A staged path:

### Step 1 — Preserve dropped edges as claims

Do not silently discard unresolved-endpoint edges.

Keep a structure like:

```text
unresolved_edge_claims
```

These do not need to affect score immediately. First, log them.

### Step 2 — Add one-anchor edge recovery

For edges where one endpoint resolved and the other did not, compare against reference edges with the same resolved anchor and edge type.

Example:

```text
resolved proc.compute_growth USES unresolved equation
```

Only compare the unresolved equation to equations that `proc.compute_growth` is expected to use.

This limits false positives.

### Step 3 — Add equation claim alignment

Implement deterministic match types:

```text
exact_symbolic
symbolic_equivalent
solved_form_of
numeric_instance_of
final_value_match
known_error_pattern
```

This directly handles case-3.

### Step 4 — Add semantic contracts for non-equation nodes

For definitions, simplifications, and conditions, add semantic contracts rather than only aliases.

Use the semantic judge to classify:

```text
entails
partially_entails
contradicts
unrelated
```

with missing/wrong parts.

### Step 5 — Feed claim alignments into node and edge scores

Let node coverage and edge coverage be computed from alignment scores, not only exact resolved keys.

Example:

```text
node coverage for eq.growth_rate = max alignment score from student claims
edge usage for proc.compute_growth USES eq.growth_rate = score of best aligned student USES claim
```

### Step 6 — Audit and calibrate

Before making this student-facing, log:

```text
old exact score
new claim-alignment score
diff
alignment basis
false positive/false negative cases
```

Then calibrate thresholds.

---

## Regression tests

Use the known failure cases:

```text
1. Macro growth case:
   Student computed growth using substituted values.
   Should align to eq.growth_rate as numeric_instance_of.
   Should recover USES credit.

2. Macro deflator case:
   Student says realGDP = nomGDP / (PI/100).
   Should align to eq.gdp_deflator as solved_form_of under PI = deflator.

3. Bernoulli case:
   Student says v2 = sqrt(2gh1).
   Should align to Bernoulli equation as solved_form_of / derived_form_of.

4. Negative math case:
   Student says realGDP = nomGDP * (PI/100).
   Should not receive full solved-form credit.
   Should be classified as wrong operation / wrong direction.

5. Partial text case:
   Student says price index adjusts nominal GDP.
   Should partially align to deflator simplification but not full credit.

6. Contradiction case:
   Student says nominal and real GDP are the same.
   Should contradict nominal-vs-real distinction.

7. Edge claim case:
   Student procedure USES unresolved numeric equation.
   The edge should survive as a claim and be scored through endpoint alignments.
```

---

## Open questions for the next agent

1. What should be the exact schema for `ClaimAlignment`?

2. Should alignments be persisted to Neo4j/Postgres, or remain ephemeral audit artifacts?

3. Should `resolved_key` remain binary while `alignments` become graded, or should resolution itself become n-best/probabilistic?

4. How should claim scores aggregate into existing dimensions like `coverage`, `usage`, `soundness`, and `bisimilarity`?

5. Should semantic contracts be hand-authored, auto-generated from reference nodes, or generated and then curated?

6. How do we prevent the semantic judge from over-crediting vague but related statements?

7. What thresholds should distinguish:

   * exact credit
   * high partial credit
   * low partial credit
   * contradiction
   * irrelevant

8. How should known misconceptions interact with generic wrong-part detection?

9. Should the system represent multiple alignments per student claim, or force a best alignment after scoring?

10. How do we keep the audit explainable enough for debugging and student feedback?

---

## Final thesis

The case-3 bug is a symptom of a deeper architectural issue.

Apollo currently treats grading as:

```text
parse → binary resolve → exact graph compare
```

But real student understanding is not binary. A student statement can be:

```text
partly correct
correct but in derived form
correct but implicit
wrong in one subpart
missing a required condition
contradictory to a reference claim
```

The better architecture is:

```text
parse → graded claim alignment → graded edge alignment → graph-level scoring
```

The main inversion is:

```text
Do not require canonical identity before grading meaning.
Evaluate meaning first, then decide how much graph credit the claim supports.
```

This would fix case-3 more naturally, improve non-equation resolution, make misconception detection less brittle, and move Apollo closer to how a human grader actually evaluates explanations.
