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
last_verified: 2026-07-31
stub: false
---

# prompts-parse-relevance — query-understanding prompts

Both are subject-parameterized functions consumed by `ai/main_ai.py`.

## Interface

- `parse_question_prompt(subject) -> str` — drives `main_ai.parse_question`
  (structured `ParsedTask`: `problem_type, asked_outputs, knowns, constraints,
  figure_refs`; PARSER_MODEL).
- `relevance_guard_prompt(subject, current_topic=None) -> str` — drives
  `check_question_relevance` (`full`/`partial`/`none` with `on_topic_portion` /
  `off_topic_portion`). `current_topic` (optional) names what the student is
  studying right now; when present the whole rule set anchors on "the course
  scope (its subject or the current module)" rather than the subject title.

## Invariants & gotchas

- **Live consumers**: the Orchestrator/eval path AND Apollo's "ask Hoot" aside
  lane (`apollo/conversation/hoot-bridge-reference-answer`) — that lane's
  out-of-scope refusal is decided entirely by this prompt. The live `/ask`
  scope enforcement is instead the tutor prompt — see `orchestrator`.
- **The classifier only sees what this prompt gives it** — the bare question
  plus `subject` (and `current_topic` when provided). With the placeholder
  subject "course/textbook" it guesses, and near-identical phrasings land on
  opposite sides (2026-07-30 surveillance-tools bug). Callers must pass the
  course's real `subject_name`; module-level content a course title can't
  convey (e.g. an ethics module in an MIS course) additionally needs
  `current_topic`.
- **The rules must anchor on the same scope as the topic line.** A prepended
  "currently studying" line is NOT enough: with classification rules phrased
  against the subject title alone, gpt-4o follows the rules and rejects
  current-module questions whose topic sounds like another discipline
  (2026-07-31 ethics-in-MIS aside bug — "objective vs subjective claim"
  rejected as "philosophy, not MIS"). When `current_topic` is present every
  rule judges "within the course scope (its subject or the current module)";
  keep it that way.
- The guard errs toward `partial` over `none` when in doubt (answer the student).

## Related

`main-ai`, `orchestrator`, `prompts-answer`.
