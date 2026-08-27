---
doc: apollo/overseer/topic-narrative
description: Ledger-grounded diagnostic-narrative prompt builder plus the deterministic output sanitizer.
owns:
  - apollo/overseer/topic_narrative.py
related:
  - apollo/overseer/diagnostic
  - apollo/overseer/topic-score
  - apollo/overseer/grounding
last_verified: 2026-08-24
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
- `PRAISE_FLOOR` — the credit at or above which prose may claim the student earned a topic (0.6, the lowest
  adjudication anchor meaning "landed"; unmoved when 0.3 was added beneath it 2026-08-24).
  DECLARED here since 2026-08-23 and re-exported by
  [diagnostic](diagnostic.md)'s `narrative_consistency`, which stays the public
  name: once the topic line renders a status WORD instead of a percentage, the
  prompt and the post-generation gate have to split credited from uncredited at
  the identical credit, and two literals that must agree are one literal.
- `nameable_misconception_keys(topics) -> frozenset[str]` + `MAX_NARRATIVE_REVEALS`
  — 2026-08-12 (P3.2 L3): THE shared reveal allocator, read by this builder and
  the consistency gate.

## Data flow

`diagnostic.generate_diagnostic` calls the builder with the `TopicScoreResult`,
the problem text, and (2026-07-14 grounding fix) the verbatim student transcript.
Each topic renders as one line with its credit status WORD (no number since
2026-08-23 — see the numeric-grade invariant below), a quoted
`You said:` line when it carries a gated per-attempt `evidence_span`, and any
nameable misconception's evidence + resolved flag. The canonical key is included
solely for copying into the response mapping. The system message
requires `{headline, topic_feedback: [{canonical_key, note, quote|null}],
next_step}` JSON and forbids model-generated recap text. After the LLM call,
`diagnostic.py` validates and scrubs every prose field with
`sanitize_narrative`.

## Invariants & gotchas

- **NO NUMBER THAT STANDS FOR A GRADE reaches student-facing prose
  (study-prep 2026-08-23, user ruling).** Two independent controls: (1) **the prompt, primary** — the topic line ends at the status word (the trailing `— {pct}%` is gone) and the system prompt forbids any score, percentage, points, "out of 100" or letter grade outright while explicitly welcoming SUBJECT-MATTER numbers, so the rule cannot be read as "avoid numbers" and no percentage is supplied anywhere to recite; (2) **`sanitize_narrative`, backstop** — see the scrub invariant below.
  Grading semantics are untouched: scores, credits, artifacts and grader payloads
  are unchanged, and `band` beside `letter` is [rubric](rubric.md)'s business.
- **`CREDIT CONSISTENCY` is a base-prompt rule (P2.1, 2026-08-07; renamed from
  `SCORE CONSISTENCY` 2026-08-23), not a flag.** Its JOB is unchanged — prose may
  never praise a topic the ledger did not credit (pilot complaint c2) — only its
  currency moved from percentages to status words: `covered` is the only credited
  status, `partially covered` and `missing` may not be praised, `missing` must
  have the idea named in its own note, and the headline/next step follow the same
  rule. This deliberately re-froze `_TOPIC_SYSTEM_PROMPT` — the byte-identical
  contracts below are relative to the flag-off build, not to the pre-P2.1 text.
  The prompt is the soft half; [diagnostic](diagnostic.md)'s gate enforces it in
  code.
- **The status word is chosen by CREDIT, not by `TopicCredit.status`
  (`_credit_status`).** `topic_score._credit_for_node` derives status from the
  coverage verdict and credit from `procedure_scores` independently, so `covered`
  at 0.4 credit and `missing` at 0.7 credit are both reachable. While the line
  carried the percentage, that number kept the prompt and the code gate keyed on
  the same quantity; with it gone the WORD has to carry the same meaning, so
  `credit >= PRAISE_FLOOR` → `covered`, `> 0` → `partially covered`, `== 0` →
  `missing`, and `unprobed` passes through untouched (it is not a credit verdict).
  The vocabulary is still `_status_label`'s — no parallel one was invented — and
  a parametrized property test pins word ⇔ `narrative_consistency._is_uncredited`
  across the whole credit range **for a non-Hoot topic**. **`partially covered` only became reachable from the
  adjudicator on 2026-08-24**: no anchor used to sit in `(0, 0.6)`, so before the 0.3 anchor the sole live source of
  that word was the Hoot cap below. A hedged topic that used to render "covered" at 0.6 now renders "partially
  covered" at 0.3 — the intended student-facing half of that fix. Hoot-assist is the deliberate exception on both sides: the flat `0.5` cap renders "partially covered" while `_is_uncredited` exempts it (a policy penalty, not absent evidence), so the narrator withholds praise and the gate appends no gap sentence — the old "covered — 50%" outcome without its contradiction.
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
  ledger-shaped scoring tokens (`credit 0.80`, `weight`, `dock`) via regex, never
  dropping legitimate prose like `weight = mg`. It is also why the level-4
  ceiling is leak-proof for free — and the number never enters the prompt at all,
  so it cannot be stated anyway.
- **The grade scrub is PRECISION over recall (2026-08-23).** Two tiers.
  **Frames** run everywhere and delete a number that sits next to a scoring word
  (`scored NN`, `earned NN points`, `your score is NN`, `an NN grade`,
  `worth NN points`, `puts you at NN%`). The **bare-token** tier (`NN%`,
  `NN percent`, `NN/100`, `NN out of 100`) is SENTENCE-ANCHORED: it fires only
  where a frame already fired in that sentence or the sentence carries an
  unambiguous grading word (`score(d)`, `graded`, `grader`, `rubric`,
  `credited`, `percentile`). Bare `grade`, `credit`, `mark`, `rate` and the band
  words are excluded from that anchor set on purpose — "a 5% grade" is a road,
  "an intermediate step" is a step, "the 20% discount rate" is MGMT content.
  A BARE INTEGER is never touched at all. A LETTER verdict (`earned a B+`,
  `grade is a B-`, `a B+ grade`) is caught behind the same verb/noun anchors, with
  a trailing guard so `an A380`, `a D-shaped duct` and `point B` survive. The one
  unanchored shape is a parenthetical holding nothing but a percentage (`(80%)`),
  the observed staging leak. Prose repairs run ONLY when a scrub fired, so
  untouched text is returned byte-identical. The inventory — positives, the
  content negatives, and the pinned residuals — is
  `apollo/overseer/tests/test_topic_narrative_numbers.py`.
- **A grade statement is removed WHOLE, not carved out of.** Phrase deletion shipped broken prose on every realistic leak ("You scored 72% overall, which is a solid start." -> "which is a solid start."), so a sentence in which the frame tier or the anchored token tier fired is dropped entirely, taking its terminator with it; the neighbouring sentences survive untouched. Only the paren-only annotation keeps phrase deletion, because removing `(80%)` is provably clean. This is ONE contract with the gate's empty-field fallback below — do not change either half alone. Costs, both pinned: a content percentage sharing a sentence with a grading word loses the sentence, and so does a student quote that shares one with a grade statement.
- **QUOTED SPANS ARE EXEMPT from the grade scrub.** Everything the narrative
  quotes is the student's own words (the prompt allows a quote only from a
  verbatim `You said` span and [diagnostic](diagnostic.md) enforces exact
  equality in code), so a number inside `"…"` is subject-matter content, not the
  system disclosing a grade — and scrubbing it would silently mangle, or via the
  exact-match gate silently DROP, a legitimate grounded quote. An unterminated
  quote fails CLOSED (the tail is scrubbed). The `quote` FIELD arrives without
  its delimiters, so `_gate_topic_quote` still drops rather than serves a span
  the scrub would rewrite.
