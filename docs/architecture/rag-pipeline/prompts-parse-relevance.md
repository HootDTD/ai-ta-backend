---
doc: rag-pipeline/prompts-parse-relevance
description: The two query-understanding classification prompts (parse + relevance guard).
owns:
  - ai/prompts/parse_question.py
  - ai/prompts/relevance_guard.py
related:
  - rag-pipeline/main-ai
  - rag-pipeline/orchestrator
  - rag-pipeline/prompts-answer
last_verified: 2026-07-25
stub: false
---

# prompts-parse-relevance — query-understanding prompts

Both are subject-parameterized functions consumed by `ai/main_ai.py`.

## Interface

- `parse_question_prompt(subject) -> str` — drives `main_ai.parse_question`
  (structured `ParsedTask`: `problem_type, asked_outputs, knowns, constraints,
  figure_refs`; PARSER_MODEL).
- `relevance_guard_prompt(subject) -> str` — drives `check_question_relevance`
  (`full`/`partial`/`none` with `on_topic_portion` / `off_topic_portion`).

## Invariants & gotchas

- **`relevance_guard` is used only by the Orchestrator/eval path**, NOT the live
  `/ask` (live scope enforcement is the tutor prompt) — see `orchestrator`.
- The guard errs toward `partial` over `none` when in doubt (answer the student).

## Related

`main-ai`, `orchestrator`, `prompts-answer`.
