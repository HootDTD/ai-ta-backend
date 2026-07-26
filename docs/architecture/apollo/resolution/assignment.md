---
doc: apollo/resolution/assignment
description: greedy_global_assignment — bounded greedy global assignment of student nodes to candidates (§5 step 4).
owns:
  - apollo/resolution/assignment.py
related:
  - apollo/resolution/resolver
last_verified: 2026-07-25
stub: false
---

# Resolution — assignment

> Part of the §5 resolver, currently **unwired** (see [_index](_index.md)).

Bounded greedy global assignment: each student node takes its single best
candidate, greedily in descending score order. Sufficient at v1 scale (~15-25
candidates).

## Interface

- `greedy_global_assignment(matches_by_node, *, cap=MAX_STUDENT_NODES)
  → AssignmentOutcome`.
- `AssignmentOutcome` (frozen) — `assignment: dict[node_id, ScoredMatch]`,
  `abstained: bool`.
- `MAX_STUDENT_NODES` (150) — the bounded-assignment cap.

## Data flow

Over the cap → abstain (empty assignment). Otherwise pick each node's single
best match (deterministic tie-break on `(node_id, canonical_key)`) and order the
result by descending score for replayability.

## Invariants & gotchas

- **One student node never splits** across targets — it takes its single best.
- **Many student nodes MAY merge** into one candidate (converging paraphrase
  evidence) — candidates are not consumed.
- **Over-cap → ABSTENTION**, never an unbounded solve that could hang the Done
  path.
- Pure + deterministic: two runs on the same input produce identical
  assignments.

## Related

- [resolution/resolver](resolver.md) — supplies `matches_by_node` and consumes
  the outcome.
