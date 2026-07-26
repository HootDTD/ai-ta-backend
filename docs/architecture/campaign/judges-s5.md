---
doc: campaign/judges-s5
description: S5 misconception-assertion judge — the precision-focused misconception gate plus reported (not gated) recall.
owns:
  - campaign/judges/s5_misconceptions.py
related:
  - campaign/judges-base
  - campaign/cast-student
  - campaign/cast-personas
last_verified: 2026-07-25
stub: false
---

# campaign/judges-s5 — misconception audit

Its own doc because misconception recall is the recurring campaign lever.

## Interface

- `S5MisconceptionJudge` (`StageJudge`) — one item per asserted misconception
  (triggering student utterance + the bank entry it matched). Gate (E3):
  **precision ≥90%** (`ok` = "the utterance really displays that specific wrong
  belief — not an unclear statement, not a DIFFERENT misconception").
- `misconception_recall(attempts) -> dict` — pure code-side recall: for each
  attempt, which of the persona's `expected.misconceptions` keys the grader
  actually asserted (`found`/`missed`/`recall` per attempt + `overall_recall`).
  Exposed via `JudgeResult.extra`, **reported, NOT gated** (spec defers
  misconception-recall targets).

## Invariants & gotchas

- Precision is gated; recall is reported only. The misconception key universe
  comes from [cast-personas](cast-personas.md)`.misconception_keys_for`; the
  attempt data comes from [cast-student](cast-student.md).

## Related

- [judges-base](judges-base.md), [cast-student](cast-student.md),
  [cast-personas](cast-personas.md).
