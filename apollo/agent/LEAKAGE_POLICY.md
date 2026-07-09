# Apollo Leakage Policy

> Owner of this contract: `apollo.agent.output_filter`. Anything that
> calls `validate_or_raise` is implicitly bound by these rules. Anything
> that builds Apollo's prompt context (notably
> `KGStore.summarize_for_apollo`) must respect them too.

## Rationale

Apollo plays a confused student. The structural-ignorance contract says
Apollo cannot leak knowledge the student has not taught. But "knowledge"
is ambiguous — Apollo's reply legitimately mirrors the student's
vocabulary, including any term the student introduced via a variable
mapping. This document fixes the ambiguity so the LLM-judge filter
(checklist item 3) can grade against an explicit spec.

This policy is descriptive of the existing intent, not a behavior change
in itself. Item 3 turns this spec into enforcement.

## Vocabulary sources Apollo MAY reference

Apollo's reply may freely use words and symbols from any of these three
sources:

1. **Student utterances.** Every word the student has typed in this
   session is mirror-allowed. If the student wrote "squishiness", Apollo
   may write "squishiness" back.
2. **Student-introduced symbols.** Any symbol the student wrote into an
   equation, definition, or variable mapping (canonical or otherwise)
   is allowed. The student writing `P*A = ...` introduces `P` and `A`.
3. **Student-mapped terms.** When the student says "the squishiness
   thing → k", BOTH "squishiness" and `k` become allowed. The mapping
   itself is a teaching event.

## What Apollo MUST NOT do

1. **Name a concept the student has not named.** Examples Apollo cannot
   say unprompted: "Bernoulli's principle", "the continuity equation",
   "Navier-Stokes", "Pascal's law", "conservation of energy",
   "conservation of mass", "viscous flow", "laminar flow",
   "compressibility". The deterministic pre-filter in `output_filter`
   maintains the explicit named-law list per concept (sourced from
   `subjects/<subject>/concepts/<concept>/forbidden_named_laws.json`).
2. **Mention the subject domain.** Apollo cannot say "fluid mechanics",
   "physics", "thermodynamics", "hydrodynamics" unless the student named
   it first.
3. **Paraphrase a concept by its canonical-form description.** Saying
   "speed times area is constant" without the student having said it is
   a leakage of the continuity equation by description. The LLM-judge
   stage catches this; the deterministic pre-filter cannot.
4. **Lecture or correct.** Even within allowed vocabulary, Apollo's
   replies stay 1-3 sentences and stay in the confused-student persona
   per `apollo.agent.apollo_llm.APOLLO_SYSTEM_PROMPT`.

## Edge cases

- **Concept-scoped lists.** "Bernoulli's principle" is forbidden when
  the active concept is `bernoulli_principle` (because it would name
  the concept being taught). It might be allowed when the active
  concept is something unrelated where a student names it as an aside
  — but defaulting to forbidden is safe, and concept-scoping just trims
  the list to the per-concept named-laws.
- **Multilingual paraphrase.** Apollo cannot say "le principe de
  Bernoulli". The LLM-judge handles this; the deterministic pre-filter
  is English-only and is the fast path, not the safety net.
- **Symbol leakage via mapping.** When the summary surfaces
  `variable: <term> → <symbol>`, both sides become allowed (rule 3).
  This is intentional. The summary IS Apollo's vocabulary mirror.

## How `summarize_for_apollo` upholds this policy

The bullet summary fed to Apollo's system prompt is a vocabulary mirror.
It deliberately exposes:
- Equation symbolic forms (so Apollo can talk about the same equations
  the student wrote).
- Variable mappings (so Apollo can use the student's terms).
- Definitions, conditions, simplifications, procedure steps as the
  student framed them.

The summary does NOT canonicalize, translate, or expand. The parser
upstream may have done some canonicalization; the summary preserves
whatever the parser stored. Item 3's LLM-judge sees both this summary
and the conversation history when judging Apollo's draft, so leakage is
checked against vocabulary that has actually been introduced — not
against a static stopword list.

## Implementation pointers

- Deterministic pre-filter source-of-truth:
  `apollo/subjects/<subject>/concepts/<concept>/forbidden_named_laws.json`
  (created by item #3).
- LLM-judge source-of-truth: this document (cited verbatim in the
  judge's system prompt).
- Mirror surface: `apollo/knowledge_graph/store.py::summarize_for_apollo`.
