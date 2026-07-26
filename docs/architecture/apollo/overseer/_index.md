---
doc: apollo/overseer/_index
description: Router for Apollo's grading, scoring, narrative, XP, and selection brains — home of the grading-path invariants.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Apollo overseer — grading, scoring & selection

The graders, the score→letter→narrative chain, XP, and problem selection. The
live Done grade is assembled by
[conversation/handlers/done.md](../conversation/handlers/done.md) — see the
recipe below.

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [transcript-coverage](transcript-coverage.md) | LIVE grader of record: one-call transcript adjudication + span gate | `apollo/overseer/transcript_coverage.py`, `apollo/overseer/coverage_contract.py`, `apollo/overseer/__init__.py` |
| [topic-score](topic-score.md) | LIVE grade of record: coverage-weighted topic score + the one serializer | `apollo/overseer/topic_score.py`, `apollo/overseer/topic_score_serialize.py` |
| [rubric](rubric.md) | Axis-weighted grade + `score_to_letter` + misconception value objects | `apollo/overseer/rubric.py`, `apollo/overseer/misconception.py` |
| [topic-narrative](topic-narrative.md) | Ledger-grounded narrative prompt + output sanitizer | `apollo/overseer/topic_narrative.py` |
| [diagnostic](diagnostic.md) | Explains (never decides) the grade; delegates to the topic narrative | `apollo/overseer/diagnostic.py` |
| [grounding](grounding.md) | INTERACTION2: session bundle → one capped, student-safe course-evidence block | `apollo/overseer/grounding.py` |
| [xp](xp.md) | XP formula + 5-tier level table + progress envelope | `apollo/overseer/xp.py` |
| [concept-inference](concept-inference.md) | Transcript → one course `concept_id` (selection only) | `apollo/overseer/concept_inference.py` |
| [problem-selector](problem-selector.md) | Tier-2 bank selection (+ personalization flag) | `apollo/overseer/problem_selector.py`, `apollo/overseer/personalization_flag.py` |
| [coverage](coverage.md) | DORMANT V3 KG-vs-KG matcher — no runtime caller | `apollo/overseer/coverage.py` |

## Cross-cutting invariants (grading path)

- **One live grading lane.** `transcript_coverage` is the sole live grader;
  `coverage.py` (V3 KG-vs-KG) has no runtime caller.
- **Grade of record = the topic score.** `done.py` replaces `rubric["overall"]`
  with the topic score/letter; XP and the served `topics` derive from it. The
  axis `rubric` blend lives on only as the legacy `scores.composite` /
  `GradingRun.composite_score` (scorecard band + mastery EWMA); it is never the
  served grade, and the module `apollo.grading.composite` does not exist.
- **Misconceptions retired.** Every topic carries an empty `misconceptions`
  tuple; the detector is gone, the shape kept for UI back-compat.
- **Flow:** transcript coverage → rubric + topic score → diagnostic/topic
  narrative → XP.
- **Course grounding is strictly additive (INTERACTION2, default OFF).**
  [grounding](grounding.md) renders the session bundle into ONE capped block
  that reframes the adjudication and narrative prompts; `None` — flag off, NULL
  bundle, corrupt bundle, or nothing student-safe — reproduces both prompts BYTE
  FOR BYTE, so an off flag cannot move a grade. Evidence is always the only
  thing truncated (it arrives pre-capped; the transcript is never trimmed for
  it), it never widens the span gate (spans stay transcript-only — a span must
  prove the STUDENT said it), and it never introduces a hard failure ahead of
  the `CoverageGradingError` -> 503 contract.

## Grading-path recipe (to change the grade, also touch)

`done.py` orchestrates: transcript-coverage → rubric → topic-score →
(diagnostic / topic-narrative, xp) → grading-artifact-writer → scorecard →
mastery. A grader change reconciles
[done.md](../conversation/handlers/done.md) plus the directional `related:`
chain transcript-coverage ↔ rubric ↔ topic-score ↔ done ↔
grading-artifact-writer ↔ scorecard ↔ mastery.
