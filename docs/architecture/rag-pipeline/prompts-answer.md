---
doc: rag-pipeline/prompts-answer
description: The two answer-generation prompts — the product's never-hallucinate / always-cite contract.
owns:
  - ai/prompts/tutor.py
  - ai/prompts/score_and_answer_snippet.py
  - ai/prompts/apollo_aside.py
related:
  - rag-pipeline/main-ai
  - rag-pipeline/context-packer
  - apollo/conversation/hoot-bridge-reference-answer
last_verified: 2026-07-28
stub: false
---

# prompts-answer — the answer-generation prompts

The most load-bearing prompts in the pipeline; they encode the product's #1/#2
priorities (never hallucinate, always cite). Consumer: `ai/main_ai.py`.

## Interface

- `tutor.py` — `TUTOR_PROMPT` constant + `tutor_prompt()` (~190 lines): the tutor
  system prompt carrying the non-negotiable answer contract — source-boundedness
  (no outside knowledge), claim-level citation discipline (exact source markers),
  per-question-type structure/length rules, a RELEVANCE CHECK + `not_relevant`
  short-circuit, Check-Your-Understanding rules, and the structured JSON output
  shape `{not_relevant, steps (single Markdown string), final_answers,
  equations_used, assumptions}`.
- `score_and_answer_snippet.py` — `SCORE_AND_ANSWER_SNIPPET_PROMPT` +
  `score_and_answer_snippet_prompt()`: the merged per-snippet citation scorer +
  answer extractor (intent-aware scoring; base everything strictly on
  `snippet_text`) driving `main_ai._score_and_answer_snippet`.
- `apollo_aside.py` — `APOLLO_ASIDE_PROMPT` + `apollo_aside_prompt()`: the compact
  aside refresher for the INTERACTION4 "Ask Hoot" lookup, passed as
  `solve_with_bundle(system_prompt_override=…)` by
  [hoot-bridge-reference-answer](../apollo/conversation/hoot-bridge-reference-answer.md).
  Keeps the tutor prompt's source-boundedness / claim-level citation / scope / LaTeX
  rules and the `{not_relevant, steps}` JSON contract, but drops all standalone-chat
  structure (headings, Check-Your-Understanding, Key-Takeaway, length tables) AND the
  tutor prompt's copy-the-excerpt-wording pressure — the aside is one flowing
  spoken-tutor explanation (60-150-word plain prose) governed by three voice rules:
  say each fact **exactly once** (synthesize overlapping excerpts into one sentence
  citing both, never restate a claim in different words), **no narrator voice** (never
  "This course material states…"/"According to the excerpt…" — say the thing and
  attach the citation), and a conversational hand-back close that carries **no**
  citation marker. Includes an in-prompt good-vs-bad example as the anti-repetition
  lever.

## Invariants & gotchas

- The tutor contract forbids numeric computation and pins `final_answers` to `{}`
  (conceptual-only), matching `main-ai`'s enforcement.
- `steps` MUST be a single Markdown string, never an array; all math in LaTeX.
- **Aside vs. the shared user payload.** `main-ai._prepare_solve_prompt` builds a
  byte-identical user payload regardless of `system_prompt_override` (locked by
  `tests/functions-tests/test_aside_prompt_override.py`), and that payload hardcodes
  the tutor three-section instruction (`## Answer` / `## Key Takeaway` /
  `## Check Your Understanding`). The aside cannot diverge the payload, so
  `apollo_aside.py` opens with an explicit `OUTPUT FORMAT OVERRIDE` that names those
  three sections and countermands them — without it the solver model intermittently
  followed the payload and emitted tutor-structured asides. Any future change to the
  payload's section wording must be mirrored in that override block.

## Related

`main-ai` (`solve_with_bundle`, `format_answer`), `context-packer` (marker
contract).
