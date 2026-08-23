---
doc: apollo/overseer/topic-narrative
description: Ledger-grounded diagnostic-narrative prompt builder plus the deterministic output sanitizer.
owns:
  - apollo/overseer/topic_narrative.py
related:
  - apollo/overseer/diagnostic
  - apollo/overseer/topic-score
  - apollo/overseer/grounding
last_verified: 2026-08-12
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
- `humanize_key(key) -> str` — public since 2026-08-07: the same student-facing
  display fallback the P2.1 consistency gate needs (see
  [diagnostic](diagnostic.md)). Canonical keys otherwise appear only as
  response identifiers.
- `nameable_misconception_keys(topics) -> frozenset[str]` + `MAX_NARRATIVE_REVEALS`
  — 2026-08-12 (P3.2 L3): THE shared reveal allocator, read by this builder and
  the consistency gate.

## Data flow

`diagnostic.generate_diagnostic` calls the builder with the `TopicScoreResult`,
the problem text, and (2026-07-14 grounding fix) the verbatim student transcript.
Each topic renders as one line with its status, whole-number percent, a quoted
`You said:` line when it carries a gated per-attempt `evidence_span`, and any
nameable misconception's evidence + resolved flag. The canonical key is included
solely for copying into the response mapping. The system message
requires `{headline, topic_feedback: [{canonical_key, note, quote|null}],
next_step}` JSON and forbids model-generated recap text. After the LLM call,
`diagnostic.py` validates and scrubs every prose field with
`sanitize_narrative`.

## Invariants & gotchas

- **`SCORE CONSISTENCY` is a base-prompt rule (P2.1, 2026-08-07), not a flag.**
  A topic below 60% may not be praised, a topic at 0% must have the missing idea
  named in its own note, and the headline/next step follow the same rule. This
  deliberately re-froze `_TOPIC_SYSTEM_PROMPT` — the byte-identical contracts
  below are relative to the flag-off build, not to the pre-P2.1 text. The prompt
  is the soft half; [diagnostic](diagnostic.md)'s gate enforces it in code.
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
- **A misconception line is named only at the corroborated rung, inside ONE
  shared reveal budget (P3.2 L3, 2026-08-12).** The `Misconception
  (corrected|uncorrected): "span"` copy and the "say nothing at all when none are
  supplied" rule both predate P3.2; populating `topics[].misconceptions` activates
  them, and `handlers/done.py` fills that container only at
  `APOLLO_WRONGNESS_LEVEL >= 3` and only from **corroborated** findings (AST-pinned
  by `test_only_corroborated_findings_are_nameable`). `nameable_misconception_keys`
  caps rendered lines at `MAX_NARRATIVE_REVEALS`
  (== `topic_score.MAX_REFERENCE_TEXT_REVEALS`) and [diagnostic](diagnostic.md)'s
  gate spends only what is left, so the channels share one budget of two rather
  than owning two each; the allocation is a pure function of the topic profile, so
  best-grade-wins retries accumulate nothing new. `unprobed` topics are excluded
  here as well as by `graded_topics_only`. A rendered line also appends
  `_MISCONCEPTION_RULES`, labelling the quoted student text as untrusted DATA (the
  W1-B/W2-A idiom) and telling the narrator a flagged topic keeps its credit. No
  rendered line → BOTH messages byte-identical: every attempt below level 3.
- **`sanitize_narrative` is belt-and-suspenders:** it strips canonical keys and
  ledger-shaped scoring tokens (`credit 0.80`, `weight`, `dock`) via regex while
  deliberately preserving whole-number percentages; never drops legitimate prose
  like `weight = mg`. It is also why the level-4 ceiling is leak-proof for free —
  and the number never enters the prompt at all, so it cannot be stated anyway.
