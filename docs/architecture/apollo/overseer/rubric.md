---
doc: apollo/overseer/rubric
description: Pure weighted-axis grade computation, the letter-band map, and the retained misconception signal value objects.
owns:
  - apollo/overseer/rubric.py
  - apollo/overseer/misconception.py
related:
  - apollo/overseer/topic-score
  - apollo/overseer/transcript-coverage
  - apollo/conversation/handlers/done
last_verified: 2026-07-25
stub: false
---

# Overseer rubric — weighted axis grade + letter bands

Deterministic, no-LLM grade over a coverage verdict + reference `Node` list. It
still runs live (`done.py`), but its `overall` is only the artifact's legacy
`composite`; the served grade is the [topic score](topic-score.md) (see
[_index](_index.md)).

## Interface

- `compute_rubric(coverage, reference_nodes, *, misconception_scores=None) ->
  dict` — per-axis `{score, letter, present}` blocks + `overall`.
- `score_to_letter(score: int) -> str` — the 0-100 → letter-band map (imported by
  [topic-score](topic-score.md)).
- From `misconception.py`: `MisconceptionSignal` (frozen per-turn signal),
  `MisconceptionState`, `summarize_for_rubric` (reduces turn-ordered signals to
  per-bank-code axis scores). `MisconceptionSignal` is also imported by the
  vestigial `agent/output_filter.py`.

## Data flow

`done.py` builds `misconception_scores` from tutoring-message metadata via
`summarize_for_rubric`, then calls `compute_rubric(coverage,
reference_graph.nodes, misconception_scores=...)`. Axes bucket reference nodes by
`node_type`: Procedure (mean of `procedure_scores`), Justification (% condition
covered), Simplification (% simplification covered), Misconception (mean of
per-code resolution scores). An absent axis redistributes its weight
proportionally; no axis present → overall 0.

## Invariants & gotchas

- **Weights are 60/25/15 rebalanced by ×0.95 with a 5% misconception axis**
  (`AXIS_WEIGHTS`). When no misconception fired the axis is absent and the
  redistribution restores an exact 60/25/15 — byte-identical to the pre-P2.8
  rubric.
- **`misconception_scores` values:** `1.0` = detected and resolved, `0.5` =
  detected and unresolved (resolution judged over the last `resolved_window=2`
  turns); never-detected codes are simply absent (no penalty).
- **Retired paths are gone.** Misconception inference and the authored bank no
  longer exist; only the `MisconceptionSignal` shape + `summarize_for_rubric`
  survive for the rubric axis and the output-filter contract.
- `_finite_score` clamps NaN/inf/non-numeric to 0.

## Related

`overall` feeds `grading/artifact-build`'s `scores.composite`; the served grade
replaces it with the topic score in `done.py`.
