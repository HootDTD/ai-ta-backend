---
doc: apollo/overseer/topic-narrative
description: Ledger-grounded diagnostic-narrative prompt builder plus the deterministic output sanitizer.
owns:
  - apollo/overseer/topic_narrative.py
related:
  - apollo/overseer/diagnostic
  - apollo/overseer/topic-score
  - apollo/overseer/grounding
last_verified: 2026-07-28
stub: false
---

# Overseer topic narrative — ledger-grounded prompt

Builds the diagnostic-narrative prompt entirely from an already-computed
`TopicScoreResult`, so every claim is traceable to a topic/misconception the
ledger actually holds. Replaces the axis-based narrative whenever a topic score
is available (closed the staging session-43 hallucination class). Pure — no IO,
no LLM call; the caller ([diagnostic](diagnostic.md)) runs the strict
structured-JSON completion.

## Interface

- `build_topic_narrative_prompt(result, *, problem_text, student_utterances=(),
  course_evidence=None) -> (system, user)` — the `(system, user)` message pair.
- `sanitize_narrative(text, canonical_keys=()) -> str` — deterministic,
  idempotent output-side gate.

## Data flow

`diagnostic.generate_diagnostic` calls the builder with the `TopicScoreResult`,
the problem text, and (2026-07-14 grounding fix) the verbatim student transcript.
Each topic renders as one line with its status, whole-number percent, and — when
the topic carries a gated per-attempt `evidence_span` — a quoted `You said:`
line; misconception lines add evidence + resolved flag. The canonical key is
included solely for copying into the response mapping. The system message
requires `{headline, topic_feedback: [{canonical_key, note, quote|null}],
next_step}` JSON and forbids model-generated recap text. After the LLM call,
`diagnostic.py` validates and scrubs every prose field with
`sanitize_narrative`.

## Invariants & gotchas

- **Reference wording is never attributed to the student** unless it appears
  verbatim in a quoted `You said:` line; a topic with no gated span is credited
  in general terms only. Topic descriptions are the reference solution's wording,
  flagged as such in the prompt.
- **Quotes are whole-span only:** the prompt requires the entire supplied
  `You said` span character-for-character or null; `diagnostic.py` enforces the
  same rule in code.
- **`student_utterances` empty (default) omits the transcript block**, preserving
  the pre-grounding input behavior.
- **`course_evidence` (INTERACTION2) is student-safe course material, NOT the
  student's words.** When supplied it is inserted BEFORE the transcript block —
  so the student's own words stay last and most salient — and the system prompt
  gains the `COURSE MATERIALS` rules: cite the bracketed marker verbatim (at
  most one per sentence, never invented), treat the excerpts as untrusted data,
  and never put excerpt text in a `quote` field or let it change a supplied
  status/percentage. `None`/empty (the default) keeps BOTH messages
  byte-identical to the ungrounded build; the block itself arrives pre-capped
  from [grounding](grounding.md), so this builder never trims anything.
- **Hoot-assisted topics add an encouraging note, never a claim (INTERACTION5).**
  When one or more supplied topics carry the ledger's `hoot_assisted` flag, the
  user message gains a `Topics Hoot answered for you` block naming ONLY those
  topics and the system prompt gains `_HOOT_ASSIST_RULES` — one short,
  student-voiced clause per assisted topic ("you looked this up with Hoot, so it
  counted for less"). Hoot's lookup content is NEVER presented as the student's
  understanding and never enters a `quote` field; the verbatim `You said` quote
  gate is unchanged. No assisted topic (the default) → BOTH messages byte-identical.
- **`sanitize_narrative` is belt-and-suspenders:** it strips canonical keys and
  ledger-shaped scoring tokens (`credit 0.80`, `weight`, `dock`) via regex while
  deliberately preserving whole-number percentages; never drops legitimate prose
  like `weight = mg`.
- `_humanize_key` degrades a snake_case key to a readable display fallback;
  canonical keys otherwise appear only as response identifiers.
