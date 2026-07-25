---
doc: ai-ta-backend/apollo/conversation/handlers/done-turn-order
description: apollo/handlers/done_turn_order.py — VESTIGIAL WU-4C1 shadow-chain turn order, orphaned by the A7 ruling (deletion candidate)
owns:
  - apollo/handlers/done_turn_order.py
related:
  - ai-ta-backend/apollo/conversation/handlers/done
  - ai-ta-backend/apollo/knowledge-graph/store
last_verified: 2026-07-25
stub: false
---

# handlers/done-turn-order — VESTIGIAL

> **DELETION CANDIDATE (D19 / Risk R1).** Orphaned by the A7 ruling that removed
> the Done shadow-grader chain. `handlers/done` does NOT import this module; the
> only surviving reference is a docstring mention in `knowledge-graph/store`.

`apollo/handlers/done_turn_order.py` was the WU-4C1 turn-order sourcing for the
Done SHADOW chain: it produced a `node_id → int` map monotone in extraction
order for `convert_findings_to_events`, since `read_graph` strips the per-write
`created_at` signal.

## Interface

None live. Historical surface: `build_turn_order(db, neo, *, attempt_id,
student_graph) -> dict[str, int]`; helpers `_read_student_turn_points`,
`_position_for`.

## Invariants & gotchas

- Algorithm (recorded for the owner): read each node's Neo4j `created_at` via the
  tiny `KGStore.read_node_created_at`, group node ids by distinct `created_at`,
  and assign each group the `turn_index` of the latest student message at-or-before
  it (else the ascending ordinal). Monotone by construction; pure over two reads.
- Consistent with the A7 ruling (shadow chain removed) — the file was simply left
  behind. Recommend deletion.

## Related

Former host: `handlers/done`; the surviving docstring reference lives in
`knowledge-graph/store`.
