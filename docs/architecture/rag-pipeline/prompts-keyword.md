---
doc: rag-pipeline/prompts-keyword
description: The keyword-stage prompt catalog plus the prompts package barrel.
owns:
  - ai/prompts/concept_extraction.py
  - ai/prompts/extract_keywords.py
  - ai/prompts/keyword_generation.py
  - ai/prompts/keyword_scoring.py
  - ai/prompts/general_term_filter.py
  - ai/prompts/synonyms.py
  - ai/prompts/__init__.py
related:
  - rag-pipeline/main-ai
  - rag-pipeline/prompts-parse-relevance
  - rag-pipeline/prompts-answer
last_verified: 2026-07-28
stub: false
---

# prompts-keyword — query-expansion prompt catalog

A cohesive catalog of the query-expansion / keyword-stage prompt modules (each a
tiny prompt-string constant or `prompt(subject)` function) — a deliberate
cohesive-catalog exception to the 1-2-file rule. Consumer: `ai/main_ai.py` only.

## Interface

| Module | Prompt | Feeds |
|---|---|---|
| `concept_extraction.py` | `concept_extraction_prompt(subject)` | the live `extract_and_filter_keywords` (textbook-index concepts, ≤8, ranked) |
| `extract_keywords.py` | `extract_keywords_prompt(subject)` | core-principle keyword extraction |
| `keyword_generation.py` | `keyword_generation_prompt()` / `KEYWORD_GENERATION_PROMPT` | candidate lookup terms (≤20, single lowercase words) |
| `keyword_scoring.py` | `keyword_scoring_prompt(subject)` | unique 0-1 ranking of candidates |
| `general_term_filter.py` | `general_term_filter_prompt(subject)` | drop generic/noisy terms (lenient) |
| `synonyms.py` | `synonyms_prompt(subject)` | 0-2 alternate keywords/abbreviations per term |

## Invariants & gotchas

- **`ai/prompts/__init__.py` is the barrel** — it re-exports ALL prompt functions
  across all three prompt docs (`relevance_guard_prompt`,
  `score_and_answer_snippet_prompt`, `parse_question_prompt`, `tutor_prompt`,
  `apollo_aside_prompt`, plus the six above). Editing any prompt doc's public
  surface must keep this barrel in sync.

## Related

`main-ai` (sole consumer), `prompts-parse-relevance`, `prompts-answer`.
