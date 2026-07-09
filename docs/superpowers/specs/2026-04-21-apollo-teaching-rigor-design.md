# Apollo Teaching Rigor — Design Spec

**Date:** 2026-04-21
**Branch:** ApolloV2
**Status:** Draft awaiting user approval

## 1. Problem Framing

Apollo today passes any student whose equations the SymPy solver can grind through, even if the student never explained *how* to use them. Coverage flags missing conditions and simplifications, but the diagnostic presents them as side notes next to "Apollo solved it ✓" — so solver success dominates the felt grade.

Procedure and chaining (the #1 pedagogy signal: *"first apply continuity to get v2, then plug into Bernoulli for P2"*) is not currently a KG entity type at all. The student can pass the numeric check by dumping equations; the diagnostic narrates the gaps but doesn't visibly punish them.

**What we're fixing:**

1. Add `procedure_step` as a first-class KG entry type, extracted by the parser, stored in KGStore, graded by coverage.
2. Author `procedure_step[]` entries into every problem's reference solution.
3. Replace the diagnostic's headline with a weighted rubric across four axes — Procedure (heaviest), Justification, Simplification, Variables — producing a letter grade. Solver success becomes a side indicator, not the verdict.
4. Strengthen Apollo's opener so the student knows from turn zero they are being graded on procedure, not just equations.
5. **Rewrite Apollo's system prompt** to replace the "probe for clarifications" instruction with in-character confusion as the default behavior:
   - Remove the explicit "Probe for clarifications, definitions, and reasons."
   - Add behavior like: *"When the student gives you equations without telling you how to use them, express genuine confusion about what to do first. Say things like 'I have these equations but I don't know which one to start with' or 'Once I have v2, what do I do with it?' You are a stuck student, not an interviewer — don't ask questions about the physics, ask about the plan."*
   - Ungate the chain-break behavior so Apollo can volunteer it, not only when directly asked.
   - Keep the existing ignorance contract, uncertainty bias, and 1-3 sentence length cap.
6. **Gamification layer (designed now, shipped in a follow-on slice):** XP = overall_score × difficulty_multiplier, cosmetic 5-tier level progression with titles and avatar variants, student freely picks difficulty every session.

**What we're NOT changing:**

- Apollo's ignorance contract or output filter discipline (no physics-term leakage).
- Parser schema for existing entry types (equation, condition, simplification, definition, variable_mapping). We're only *adding* `procedure_step`.
- The `from_hoot` handoff or session lifecycle.
- The split between Apollo context and Overseer context — the Overseer still sees everything, Apollo still sees only what the student taught and their conversation.
- The pure-SymPy solver — still no LLM in the solve path.

## 2. Architecture & Components

### Backend: schemas & authoring (new)

- New Pydantic model `ProcedureStep { order: int, action: str, uses_equations: list[str], purpose: str }`. Lives in `apollo/schemas/procedure.py` (matching the one-module-per-schema pattern already in use).
- Extend each problem JSON in `apollo/problems/bernoulli/*.json` with a `procedure_step[]` array inside `reference_solution`. All 5 existing problems are re-authored once during rollout. One-time cost.
- Add `apollo/problems/AUTHORING.md` with a worked Bernoulli `procedure_step[]` example so re-authoring is templated.

### Backend: parser (modified)

- `apollo/parser/parser_llm.py`: add `procedure_step` to the system prompt's allowed entry types; specify its content fields (`order`, `action`, `uses_equations`, `purpose`); extend the `_is_non_trivial` heuristic with plan-speak markers (`first`, `then`, `next`, `after that`, `use X to get Y`) so that plan-like utterances yielding zero extractions raise `ParserCouldNotExtractError`.

### Backend: KG store (modified)

- `apollo/knowledge_graph/store.py`: ensure `write_entries`, `read_kg`, and `summarize_for_apollo` handle the new `procedure_step` type. KG rows are stored as `(type, content JSONB)` today — no DB migration, just code paths that know about the new type.

### Backend: overseer (modified)

- `apollo/overseer/coverage.py`: add `_procedure_matches` — softer than string equality because action text is free-form. Implementation: a small LLM call per reference procedure step asking "does any student procedure_step describe the same action and approximate ordering?" Returns a 0-1 score per step (not binary covered/missing) to permit partial credit. Absent-axis redistribution lives here.
- `apollo/overseer/diagnostic.py`: compute the weighted rubric (pure function, no LLM). Pass rubric scores into the existing diagnostic LLM prompt so the narrative aligns with the grade, leading with the lowest-scoring axis. Return a *structured* rubric dict alongside the narrative string.

### Backend: handler & response shape (modified)

- `apollo/handlers/done.py`: return `rubric`, `solver_indicator`, `diagnostic_narrative`, and `coverage` as separate fields. Stop returning `result` as the top-level verdict.

### Backend: Apollo persona (modified)

- `apollo/agent/apollo_llm.py`: prompt rewrite per Section 1.5.

### Frontend (modified)

- `components/apollo/ApolloReportPanel.tsx`: new rubric card layout (4 axes × letter bands + overall letter), with the solver outcome as a small badge. Diagnostic narrative renders below the card.
- `components/apollo/ApolloChat.tsx` — update the empty-state opener (the frontend's student-facing "Hi, teach me…" message) to frame procedure as the bar: *"I need you to walk me through the steps, not just give me formulas — explain what to do first, why, and how to get from there to the answer."* The backend Apollo LLM does not emit an opener; the opener is purely a frontend empty-state string.

### Phase 2: gamification (design locked, implementation follows Phase 1)

- New side table `apollo_student_progress { student_id PK, xp_total INT NOT NULL DEFAULT 0, level INT NOT NULL DEFAULT 1, last_level_up_at TIMESTAMPTZ NULL }`. Kept separate from the existing student/profile row so gamification state is isolated from identity/auth state and can be dropped/reset without touching core student records.
- XP formula lives in a new `apollo/overseer/xp.py` module.
- Frontend: XP chip and current-level title on student home; Apollo avatar cosmetic variant tied to level tier (5 tiers, see Section 5).

## 3. Data Flow on Done

1. Student hits Done. `handle_done` freezes the KG and reads it. KG now may include `procedure_step` entries alongside existing types.
2. **Solver** runs unchanged — KG equations + augmented givens + target → `{ status, value, trace, missing_variables }`.
3. **Coverage** runs over all reference entries:
   - Existing matchers for equation, condition, simplification, definition, variable_mapping.
   - **New**: `_procedure_matches` — small LLM call per reference procedure step, returning a 0-1 match score.
4. **Rubric computation** (pure function):
   - `Procedure       = mean(per_step_score) × 100`, weight **0.50**
   - `Justification   = (covered_condition / total_condition) × 100`, weight **0.25**
   - `Simplification  = (covered_simplification / total_simplification) × 100`, weight **0.125**
   - `Variables       = (covered_def_and_var_map / total_def_and_var_map) × 100`, weight **0.125**
   - Absent axes (reference has zero entries of that type) are dropped; remaining weights are proportionally rescaled to sum to 1.0.
   - `Overall = weighted_sum`, mapped to a letter band.
5. **Diagnostic LLM** receives: full coverage dict, rubric scores, solver result, reference steps, problem text. Produces narrative ("Here's what you taught well / what broke / what to re-teach"). The LLM no longer decides the verdict — it *explains* the verdict the rubric computed. Narrative must open with the lowest-scoring axis and end with a concrete next step tied to that axis.
6. **Response** from `handle_done`:
   ```json
   {
     "rubric": {
       "overall":        { "score": 78,  "letter": "B+" },
       "procedure":      { "score": 60,  "letter": "C+" },
       "justification":  { "score": 100, "letter": "A"  },
       "simplification": { "score": 100, "letter": "A"  },
       "variables":      { "score": 100, "letter": "A"  }
     },
     "solver_indicator": { "reached": true, "value": "194000 Pa" },
     "diagnostic_narrative": "...",
     "coverage": { "<step_id>": "covered" | "missing" }
   }
   ```
   Phase 2 adds: `xp_earned`, `level_before`, `level_after`, `level_up: bool`.

**Key property:** solver success no longer dominates. A student who dumps correct equations with no procedure gets an F on Procedure → C-range overall, even with `solver_indicator.reached = true`. A student who teaches excellent procedure but has one typo'd equation gets A on Procedure → A-range overall with `solver_indicator.reached = false` flagged as a side indicator.

**Design bet:** coverage completes the rubric without the LLM being in the loop for grading — the LLM only *explains*. This keeps grades deterministic, auditable, and reproducible across runs.

## 4. The Rubric

### Letter bands (applied per-axis and to overall)

| Band | Range  |
|------|--------|
| A+   | 97-100 |
| A    | 90-96  |
| A-   | 85-89  |
| B+   | 80-84  |
| B    | 75-79  |
| B-   | 70-74  |
| C+   | 65-69  |
| C    | 60-64  |
| D    | 50-59  |
| F    | 0-49   |

### Weighting & absent-axis handling

- Base weights: Procedure 0.50, Justification 0.25, Simplification 0.125, Variables 0.125.
- If the problem's reference solution has zero entries of a given type (e.g., a simple continuity problem with no required simplifications), that axis is *absent* and its weight is redistributed proportionally across the remaining axes. Procedure stays dominant in all configurations.
- A student is never docked for missing what the problem doesn't require.

### Partial credit on procedure

The procedure matcher returns a 0-1 score per reference step — not binary — because the coverage LLM can say "this student step mostly covers the reference but is vague on ordering." The axis score is the mean of per-step scores × 100. Procedure is fuzzy by nature; binary match would punish students for natural-language variance.

### Rubric card UI (conceptual)

```
┌─ Teaching grade: B+ (78) ─────────────┐
│ Procedure         C+  (60)  ████▒▒▒▒ │
│ Justification     A   (100) ████████ │
│ Simplification    A   (100) ████████ │
│ Variables         A   (100) ████████ │
│                                       │
│ Apollo reached the answer: ✓ 194 kPa │
└───────────────────────────────────────┘
```

The card sits at the top of the report. Diagnostic narrative sits below it. Narrative opens with the weakest axis — not with solver success.

### Diagnostic narrative prompt change

The narrative LLM now receives rubric scores as input and is instructed to *align its text with them* — lead with the lowest-scoring axis, explicitly name which axis the student is weak on, end with a concrete next step tied to that axis. No more generic "nice job!" openings; the narrative reflects what the rubric measured.

## 5. Gamification (Phase 2)

Designed now, implemented in the follow-on slice after Phase 1 ships.

### XP formula

```
xp_earned = floor(overall_score × difficulty_multiplier)
```

Difficulty multipliers: Beginner ×1.0, Easy ×1.25, Intermediate ×1.5, Challenging ×2.0. Max XP per session is 200 (Challenging A+). An A-grade Intermediate problem awards ~135 XP.

### Re-attempt rule

First attempt at a problem awards full XP. Re-attempts of the same problem award 25% of the computed XP. Rewards mastery, discourages grinding. No negative XP ever — a bad grade just earns less.

### Levels (cosmetic, 5 tiers)

| Level | Title              | XP threshold | Avatar cosmetic        |
|-------|--------------------|--------------|------------------------|
| 1     | Apollo Apprentice  | 0            | base owl               |
| 2     | Apollo Adept       | 300          | subtle color accent    |
| 3     | Apollo Scholar     | 800          | small accessory (scarf)|
| 4     | Apollo Sage        | 1600         | visible accent + glow  |
| 5     | Apollo Archon      | 3000         | full cosmetic variant  |

Curve: roughly ×2 per tier. At Intermediate A-grade (~135 XP/session), level 5 is ~22 clean sessions. At Challenging A (~180 XP/session), ~17 sessions. Earned without being grindy.

### Difficulty choice

Student picks difficulty every session. No gating by level. No auto-ramp. The XP multiplier is the only incentive to push to harder problems — motivation, not coercion.

### UI surfacing

- Student home: XP chip, current level title, avatar variant.
- Apollo page greeting changes with level: *"Welcome back, Apollo Apprentice"* → *"Welcome back, Apollo Archon"*.
- Done screen: grade report, then a line showing `+78 XP earned` under the rubric card, with a subtle level-up celebration animation if a threshold was crossed. Confetti, not fireworks.

### Persistence

New side table `apollo_student_progress` keyed by `student_id`, columns: `xp_total INT NOT NULL DEFAULT 0`, `level INT NOT NULL DEFAULT 1`, `last_level_up_at TIMESTAMPTZ NULL`. Row is upserted at end of `handle_done`:

```python
new_xp = old_xp + xp_earned
new_level = max(t.level for t in TIERS if t.threshold <= new_xp)
level_up = new_level > old_level
```

### Phase boundary

Nothing in Section 5 blocks Phase 1 from landing in isolation. The Done response simply omits XP/level fields until Phase 2 ships.

## 6. Error Handling & Testing

### New error modes

- **Parser extracts no procedure from a plan-like utterance.** `_is_non_trivial` gains plan-speak markers (`first`, `then`, `next`, `after that`, `use X to get Y`). A plan-like utterance yielding zero extractions raises `ParserCouldNotExtractError` — same no-fallback discipline as the rest of the parser.
- **Procedure matcher LLM call fails or times out.** Soft-fail: mark that reference step as uncovered, log, continue. Grade is still produced; the Procedure axis just scores lower on that attempt. Never block the report.
- **Malformed `procedure_step` content from parser** (missing `order`, wrong types). Pydantic validation drops the bad entry; other valid entries in the same utterance persist. No retry.
- **Backward-compat: old problem JSON without `procedure_step[]`.** Coverage treats Procedure as an absent axis and redistributes weights per Section 4. This lets us roll out incrementally without breaking existing problems.

### Boundary cases

- Empty KG → solver stuck, all axes 0, grade F, diagnostic says "you didn't teach Apollo anything." No crash.
- Procedure-only teaching (no equations) → solver stuck, Procedure axis earns, other axes 0. Grade reflects reality.
- Perfect teaching → solver solves, all axes high, rubric A-range.

### Testing additions

- `apollo/parser/tests/test_parser.py`: procedure_step extraction across plan-speak variants; raise-on-empty for plan-like utterances.
- `apollo/overseer/tests/test_coverage.py`: procedure semantic matcher (mocked LLM), 0-1 partial credit, absent-axis redistribution math.
- `apollo/overseer/tests/test_diagnostic.py`: rubric computation as a pure function — weights, letter bands, absent-axis handling — **no LLM involved in these tests**.
- `apollo/handlers/tests/test_done.py`: new response shape, rubric separate from solver_indicator, solver success doesn't force an A when procedure is missing.
- `apollo/agent/tests/test_apollo_llm.py`: prompt asserts confusion-as-default, not probe-as-default. Specifically assert `"probe for clarifications"` is no longer in the prompt.
- `apollo/tests/test_e2e_smoke.py`: updated assertions for the rubric fields.

### Phase 2 tests (gamification)

- XP formula: multipliers per difficulty, re-attempt discount, floor semantics, no negative XP.
- Level thresholds: tier mapping at boundary values (299/300, 2999/3000, etc.).
- Persistence update: `handle_done` writes `xp_total`/`level` correctly; `level_up` flag correctness.

### Authoring guidance

`apollo/problems/AUTHORING.md` gains a `procedure_step[]` section with a worked Bernoulli example. One-time re-authoring of the 5 existing problems is templated, not improvised.

## 7. Implementation Plan (Phasing)

**Phase 1 — Procedure as a first-class graded axis + weighted rubric headline.**
Schema (`ProcedureStep`), parser extension, KG store support, coverage matcher, rubric computation, diagnostic prompt + output shape, handler response shape, Apollo prompt rewrite, frontend rubric card, frontend opener rewrite, re-authored problem JSONs (all 5), updated tests.

**Phase 2 — Gamification.**
`apollo_student_progress` table, `apollo/overseer/xp.py`, XP/level fields added to Done response, frontend XP chip / level title / avatar variants / level-up animation, gamification tests.

Phases are fully decoupled. Phase 2 can land any time after Phase 1 is live and stable.
