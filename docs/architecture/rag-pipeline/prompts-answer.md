---
doc: ai-ta-backend/rag-pipeline/prompts-answer
description: The two answer-generation prompts — the product's never-hallucinate / always-cite contract.
owns:
  - ai/prompts/tutor.py
  - ai/prompts/score_and_answer_snippet.py
related:
  - ai-ta-backend/rag-pipeline/main-ai
  - ai-ta-backend/rag-pipeline/context-packer
last_verified: 2026-07-25
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

## Invariants & gotchas

- The tutor contract forbids numeric computation and pins `final_answers` to `{}`
  (conceptual-only), matching `main-ai`'s enforcement.
- `steps` MUST be a single Markdown string, never an array; all math in LaTeX.

## Related

`main-ai` (`solve_with_bundle`, `format_answer`), `context-packer` (marker
contract).
