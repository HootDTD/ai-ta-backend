---
doc: ai-ta-backend/rag-pipeline/main-ai
description: The OpenAI chat-completion brain of /ask — parse, keyword, per-snippet scoring, answer, citation formatting.
owns:
  - ai/main_ai.py
  - ai/__init__.py
related:
  - ai-ta-backend/rag-pipeline/prompts-keyword
  - ai-ta-backend/rag-pipeline/prompts-parse-relevance
  - ai-ta-backend/rag-pipeline/prompts-answer
  - ai-ta-backend/rag-pipeline/context-packer
  - ai-ta-backend/rag-pipeline/citations-formatter
  - ai-ta-backend/rag-pipeline/streaming
  - ai-ta-backend/rag-pipeline/solver
  - ai-ta-backend/rag-pipeline/orchestrator
  - ai-ta-backend/platform/config-model-pins
  - ai-ta-backend/platform/config-contracts
last_verified: 2026-07-25
stub: false
---

# main-ai — the LLM brain of /ask

Known monolith (~1868 lines, ~25 functions). server.py is the sole production
importer. `ai/__init__.py` is an EMPTY namespace package (not a re-export
facade). Documented by pipeline stage since it cannot be split at doc-write time.

## Interface

**(1) Query understanding** — `parse_question(user_query, subject=None) -> ParsedTask`;
`check_question_relevance(question, subject) -> {relevance: full|partial|none, ...}`;
`is_question_subject_relevant`.
**(2) Keyword expansion** — `extract_keywords`, `filter_keywords_by_subject`,
`filter_general_terms`, `propose_synonyms`, and the live entry
`extract_and_filter_keywords(question, subject) -> (context_summary, [{term, relevance}] max 8)`.
**(3) Per-snippet citation scoring** — `_score_and_answer_snippet` (ThreadPool,
`_citation_pool_size`), `_importance_from_snippet`, `_pick_concept_term`; blended
`0.6*relevance + 0.4*directness*importance`, dropped below `CITATION_SCORE_FLOOR`.
**(4) Answer generation** — `_prepare_solve_prompt`,
`solve_with_bundle(parsed_task, bundle, hint, subject) -> ProposedSolution` and
the streaming twin `solve_with_bundle_stream`; `_build_solution_from_data`,
`_is_reasoning_model`.
**(5) Citation formatting** — `format_answer(solution, bundle, *, include_background,
citation_label, subject) -> FinalAnswer`; `_strip_zero_width`.
**(6) Debug writers** — `_write_proof_citations`/`_write_citations_file`/`_write_miniresponses`.

## Data flow

The tutor call (`solve_with_bundle`) pins `model = config.models.MAIN_MODEL`
(`platform/config-model-pins`); reasoning models get `reasoning_effort =
MAIN_REASONING_EFFORT`, others `temperature=0`. `final_answers` is forced to `{}`
(conceptual-only — no numeric compute). `format_answer` strips zero-width chars
(gpt-5 emits literal `&#8203;`), regex-matches markers against
`bundle.allowed_markers`, strips unknown markers, rotates allowed markers onto
uncited prose paragraphs (code / `$$` / headings exempt), and appends a
`Citations:` trailer. Prompts come from the `.prompts` package.

## Invariants & gotchas

- **conceptual-only mode**: `final_answers={}`; a `not_relevant` result
  short-circuits.
- **Fail-open everywhere**: each LLM helper degrades (guard → full; keywords → []).
- **`filter_general_terms` references `WIRE`**, which is defined only in
  `ai/orchestrator.py` — a latent `NameError` swallowed by the surrounding
  try/except, which then returns the unfiltered terms (`orchestrator` owns `WIRE`).
- Imports `.solver.run_python` (dormant) and `ai.streaming.JsonStringFieldStreamer`
  (lazy, streaming path only).

## Env flags

`PARSER_MODEL`, `KEYWORD_MODEL`, `CITATION_SCORER_MODEL` (no PARSER_MODEL
fallback), `CITATION_WORKERS`, `CITATION_SCORE_FLOOR`, `PROMPT_CACHE_KEY`,
`OPENAI_SERVICE_TIER`, `MAIN_VERBOSITY`, `GENERAL_FILTER_MODE`; MAIN_MODEL /
MAIN_REASONING_EFFORT pinned in `platform/config-model-pins`.

## Related

`prompts-*`, `context-packer`, `citations-formatter`, `streaming`, `solver`,
`orchestrator`, `platform/config-model-pins`.
