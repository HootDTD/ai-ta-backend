---
doc: apollo/overseer/diagnostic
description: Generates the student-facing narrative that explains — never decides — the grade.
owns:
  - apollo/overseer/diagnostic.py
related:
  - apollo/overseer/topic-narrative
  - apollo/overseer/topic-score
  - apollo/overseer/rubric
  - apollo/conversation/handlers/done
last_verified: 2026-07-25
stub: false
---

# Overseer diagnostic — explains the verdict

`generate_diagnostic` produces the short student-facing narrative that explains
the grade. It never decides the grade — the [rubric](rubric.md) /
[topic score](topic-score.md) is the verdict; this only narrates it.

## Interface

- `generate_diagnostic(*, coverage, reference_steps, problem_text, rubric,
  model=None, topic_score=None, student_utterances=()) -> str` — imported by
  `handlers/done.py`.

## Data flow

Two prompt paths. When a `TopicScoreResult` is passed, it delegates to
[topic-narrative](topic-narrative.md)`.build_topic_narrative_prompt` (every claim
ledger-grounded). Otherwise it uses the default axis-based `_SYSTEM_PROMPT`,
leading with the lowest-scoring axis. Either way it runs one `MAIN_MODEL`
completion (temperature 0.4), appends deterministic misconception + negotiation
recap lines (read from `rubric`/`coverage`), and finally runs
`sanitize_narrative`.

## Invariants & gotchas

- **Soft-fail:** an LLM exception yields a fixed `[Diagnostic narrative
  unavailable — the grade above is still accurate.]` string, never a raise — the
  grade is independent of the narrative.
- **Attribution rules** match the topic narrative: address the student as
  "you"/"your"; never present a reference detail as something the student said.
- **Recap lines are deterministic** and appended regardless of prompt path:
  `_append_misconception_line` fires only when a misconception was detected;
  `_append_negotiation_line` only when the student negotiated ≥1 entry.
- The final `sanitize_narrative` pass is a no-op on clean prose.

## Related

`MAIN_MODEL` is pinned in `platform/config-model-pins`.
