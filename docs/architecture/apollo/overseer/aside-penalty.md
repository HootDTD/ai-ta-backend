---
doc: apollo/overseer/aside-penalty
description: INTERACTION5 pure credit-cap transform — flat-caps rubric nodes a Hoot lookup aside pre-explained, before the grade fans out.
owns:
  - apollo/overseer/aside_penalty.py
related:
  - apollo/overseer/transcript-coverage
  - apollo/overseer/topic-score
  - apollo/overseer/rubric
  - apollo/conversation/handlers/done
last_verified: 2026-07-28
stub: false
---

# Overseer aside penalty — the Hoot-assist grading cap

The INTERACTION5 flat cap. A single pure, total function applied to the coverage
verdict AFTER adjudication and BEFORE the grade fans out to its two credit
consumers — the [topic score](topic-score.md) (`_credit_for_node`, the served
grade of record) and the legacy axis [rubric](rubric.md) (`compute_rubric`) — so
both lanes see the SAME capped values. No IO, no LLM, no DB.

## Interface

- `apply_aside_caps(coverage, *, cap=0.5) -> (new_coverage, assisted_node_ids)` —
  returns a NEW `CoverageVerdict` where every node flagged in
  `coverage["hoot_assisted"]` has `procedure_scores[node] = min(effective, cap)`
  and `per_step[node] = "missing"`; `assisted_node_ids` is the sorted tuple of
  capped nodes. Absent or all-`False` `hoot_assisted` → the SAME object back and
  an empty tuple (byte-identical passthrough).

## Data flow

For each assisted node the transform reads its EFFECTIVE pre-cap credit —
`procedure_scores[node]` if present, else `1.0` when `per_step == "covered"` (a
binary covered node with no procedure score), else `0.0` — then writes
`min(effective, cap)` back and forces `per_step = "missing"`. A capped node
therefore lands `(capped, "partial")` when `capped > 0` and `(capped, "missing")`
when `capped == 0`; it can never grade `covered`. `done.py` calls this right
after coverage and before rubric/topic-score/diagnostic.

## Invariants & gotchas

- **Both maps are edited so the two lanes agree.** `procedure_scores` caps the
  continuous credit (read by the topic lane and the rubric procedure axis);
  forcing `per_step = "missing"` denies the `"covered"` status the topic lane and
  the binary justification/simplification axes key on. A one-sided edit would let
  the lanes disagree about the same node.
- **Flat cap, no earn-back.** A topic a Hoot aside explained stays capped for this
  attempt even if the student later teaches it well — the flag is judged against
  the aside text alone by the adjudicator.
- **No mutation.** A fresh top-level dict is returned with fresh copies of only
  `per_step` and `procedure_scores`; `confidences` / `negotiation_counts` /
  `hoot_assisted` are carried by reference and never touched. A malformed/NaN
  score coerces to `0.0`, mirroring the topic and rubric lanes' own clamps.

## Related

Fed by the adjudicator's `hoot_assisted` map ([transcript-coverage](transcript-coverage.md));
wired and soft-failed by [done](../conversation/handlers/done.md).
