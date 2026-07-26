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
last_verified: 2026-07-26
stub: false
---

# Overseer diagnostic — explains the verdict

`generate_diagnostic` produces the short student-facing narrative that explains
the grade. It never decides the grade — the [rubric](rubric.md) /
[topic score](topic-score.md) is the verdict; this only narrates it.

## Interface

- `generate_diagnostic(*, coverage, reference_steps, problem_text, rubric,
  model=None, topic_score=None, student_utterances=()) -> (narrative,
  feedback_or_none)` — imported by `handlers/done.py`. Topic feedback is
  `{headline, topic_feedback: [{canonical_key, note, quote}], recap,
  next_step}`.

## Data flow

Two prompt paths. When a `TopicScoreResult` is passed, it delegates to
[topic-narrative](topic-narrative.md)`.build_topic_narrative_prompt` and requests
strict structured JSON from one `MAIN_MODEL` completion. Code validates topic
keys/order, exact-gates each quote against that topic's `evidence_span`,
sanitizes every prose field, appends deterministic misconception + negotiation
entries in `recap[]`, then flattens headline → topic notes → recap → prefixed
next step for back compatibility. Otherwise it uses the unchanged axis prompt
and returns the legacy sanitized narrative plus null feedback.

## Invariants & gotchas

- **Soft-fail:** an LLM exception yields the fixed unavailable narrative; a
  JSON parse/validation failure preserves the same completion's raw text as the
  legacy narrative. Both return null feedback and never raise into grading.
- **Quotes are code-gated:** a quote survives only when it exactly equals the
  gated `evidence_span` for its canonical topic and is already sanitizer-clean;
  otherwise it becomes null.
- **Attribution rules** match the topic narrative: address the student as
  "you"/"your"; never present a reference detail as something the student said.
- **Recap lines are deterministic:** the topic path puts the existing appender
  outputs in code-owned `recap[]`; the axis/soft-fail path appends them to the
  legacy string exactly as before.
- `sanitize_narrative` runs per structured prose field and at the legacy return
  boundary.

## Related

`MAIN_MODEL` is pinned in `platform/config-model-pins`.
