---
doc: apollo/overseer/topic-narrative
description: Ledger-grounded diagnostic-narrative prompt builder plus the deterministic output sanitizer.
owns:
  - apollo/overseer/topic_narrative.py
related:
  - apollo/overseer/diagnostic
  - apollo/overseer/topic-score
last_verified: 2026-07-25
stub: false
---

# Overseer topic narrative — ledger-grounded prompt

Builds the diagnostic-narrative prompt entirely from an already-computed
`TopicScoreResult`, so every claim is traceable to a topic/misconception the
ledger actually holds. Replaces the axis-based narrative whenever a topic score
is available (closed the staging session-43 hallucination class). Pure — no IO,
no LLM call; the caller ([diagnostic](diagnostic.md)) runs the completion.

## Interface

- `build_topic_narrative_prompt(result, *, problem_text, student_utterances=())
  -> (system, user)` — the `(system, user)` message pair.
- `sanitize_narrative(text, canonical_keys=()) -> str` — deterministic,
  idempotent output-side gate.

## Data flow

`diagnostic.generate_diagnostic` calls the builder with the `TopicScoreResult`,
the problem text, and (2026-07-14 grounding fix) the verbatim student transcript.
Each topic renders as one line with its status, whole-number percent, and — when
the topic carries a gated per-attempt `evidence_span` — a quoted `You said:`
line; misconception lines add evidence + resolved flag. After the LLM call the
raw output is scrubbed by `sanitize_narrative`.

## Invariants & gotchas

- **Reference wording is never attributed to the student** unless it appears
  verbatim in a quoted `You said:` line; a topic with no gated span is credited
  in general terms only. Topic descriptions are the reference solution's wording,
  flagged as such in the prompt.
- **`student_utterances` empty (default) keeps the prompt byte-identical** to the
  pre-grounding build.
- **`sanitize_narrative` is belt-and-suspenders:** it strips canonical keys and
  ledger-shaped scoring tokens (`credit 0.80`, `weight`, `dock`) via regex while
  deliberately preserving whole-number percentages; never drops legitimate prose
  like `weight = mg`.
- `_humanize_key` degrades a snake_case key to a readable phrase so raw keys
  never reach the prompt.
