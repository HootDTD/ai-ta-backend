---
doc: apollo/overseer/_index
description: Router for Apollo's grading, scoring, narrative, XP, and selection brains — home of the grading-path invariants.
owns: []
related: []
last_verified: 2026-07-28
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
| [diagnostic](diagnostic.md) | Explains the grade (never decides) + verdict-consistency gate + optional remediation pointers | `apollo/overseer/diagnostic.py`, `apollo/overseer/remediation.py`, `apollo/overseer/narrative_consistency.py` |
| [aside-penalty](aside-penalty.md) | INTERACTION5: pure flat credit-cap for Hoot-assisted rubric nodes | `apollo/overseer/aside_penalty.py` |
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
  narrative → consistency gate (P2.1: prose never praises an uncredited topic;
  every zeroed topic that COUNTED names its gap) → remediation → XP.
- **Course grounding is strictly additive (INTERACTION2, default OFF).**
  [grounding](grounding.md) reframes the adjudication + narrative prompts with one
  capped block; `None` — flag off, NULL/corrupt bundle, nothing student-safe —
  reproduces both prompts BYTE FOR BYTE. It only truncates the pre-capped evidence
  (never the transcript), never widens the span gate, never adds a hard failure.
- **Remediation never grades.** `INTERACTION3` only decorates successful
  structured feedback in one swallowed failure domain; it cannot change score,
  letter, narrative, XP, or grade persistence.
- **Hoot-assist cap is strictly additive (INTERACTION5, default OFF).**
  [aside-penalty](aside-penalty.md) flat-caps every rubric node a Hoot lookup aside
  explained (credit ≤ 0.5, never `covered`) before the grade fans out, so rubric,
  topic score, narrative, and served topics all surface `hoot_assisted` on the same
  capped values. Own swallowed failure domain; `hoot_asides=()` is byte-identical.

## Grading-path recipe (to change the grade, also touch)

`done.py` orchestrates: transcript-coverage → rubric → topic-score → (diagnostic
/ topic-narrative / narrative-consistency, xp) → grading-artifact-writer →
scorecard → mastery. A grader change reconciles
[done.md](../conversation/handlers/done.md) plus the directional `related:` chain
transcript-coverage ↔ rubric ↔ topic-score ↔ done ↔ grading-artifact-writer ↔
scorecard ↔ mastery.
