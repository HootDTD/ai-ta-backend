---
doc: ai-ta-backend/rag-pipeline/orchestrator
description: Legacy/eval sequential state machine — imported by server.py but never instantiated on the live path.
owns:
  - ai/orchestrator.py
related:
  - ai-ta-backend/rag-pipeline/main-ai
  - ai-ta-backend/rag-pipeline/prompts-parse-relevance
last_verified: 2026-07-25
stub: false
---

# orchestrator — the legacy/eval state machine (NOT live)

Imported at server.py but **never instantiated by the live handlers** — the
production `/ask` calls `_ask_pgvector` directly. Documented so agents do not
mistake it for the live scope filter.

## Interface

- `class Orchestrator` with `run(user_task) -> FinalAnswer` — the batch/eval
  entry (parse → iterative retrieve → LLM/dump).
- Module-level `WIRE = os.getenv("RETRIEVAL_WIRE_LOG", ...)` — the gate that
  `main_ai.filter_general_terms` erroneously references (defined HERE, not there).
- `_clamp_weight` helper.

## Data flow

`run()` parses the question, then loops `_iterative_research` up to
`max_retrieval_rounds`, **doubling `token_budget` and `k_sem`/`k_lex` (capped) on
each `bundle.validate()` failure**. The **pre-retrieval relevance guard**
(`check_question_relevance`, full/partial/none, fail-open) and **partial-relevance
splitting** (`RelevanceNote`, only fires when `bundle.provenance.relevance_level
== "partial"`, set only here) live on THIS path — not the live one.

## Invariants & gotchas

- **Not live**: the live scope enforcement is instead the tutor prompt + the
  `not_relevant` short-circuit in `main-ai`.
- **EVAL_MODE hook**: with `EVAL_MODE` truthy, `run()` dumps the context pack to
  `EVAL_DUMP_PATH` (default `../system-upgraderrrr/context_packs/`) and skips the LLM.

## Env flags

`RETRIEVAL_WIRE_LOG` (the `WIRE` gate), `EVAL_MODE`, `EVAL_DUMP_PATH`,
`TERM_SEMANTIC_MODE`.

## Related

`main-ai` (live replacement), `prompts-parse-relevance` (relevance_guard).
