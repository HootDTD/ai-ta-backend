---
doc: apollo/conversation/questioning/selection
description: Deterministic question-target policy — graded-first ordering, contested-first priority, the 2-asks-per-node cap, and the graded budget reservation.
owns:
  - apollo/smart_questions/selection.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/questioning/controller
  - apollo/conversation/questioning/challenge
  - apollo/overseer/topic-score
  - apollo/ontology/graph
last_verified: 2026-08-12
stub: false
---

`apollo/smart_questions/selection.py` decides **which reference nodes Apollo may
target this turn**. It exists because both prior rules were prompt-only and both
were violated in prod: 31% of graded nodes were never probed (so they were graded
`missing`) while ~30% of the ask budget went to ungraded `definition` nodes, and
`times_asked` reached 3 despite a "max 2 asks" prompt line.

## Interface

- `build_selection_policy(*, reference_graph, tally_state=(), updates=(), questions_asked=0, cap, contested_ids=()) -> SelectionPolicy`
  — the only entry point; imported by `questioning/unified`, `questioning/controller`
  and (for `SelectionPolicy` alone) `questioning/challenge`.
- `SelectionPolicy` (frozen): `graded_ids`, `open_graded_ids`, `askable_ids`,
  `reserved_for_graded`, `graded_only`, plus the derived `graded_topic_total` /
  `open_graded_topics` counts the chat response serves to the student-ui coverage meter.
- `ordered_nodes(reference_graph)`, `is_graded(node)`, `GRADED_NODE_TYPES`, `MAX_ASKS_PER_NODE`.
- `NodeStateLike` / `NodeUpdateLike` protocols — callers pass `unified`'s
  `TallyState` / `TallyUpdate` structurally, so this module never imports
  `unified` (that would be a cycle).

## Data flow

Statuses come from `tally_state` overlaid with this turn's `updates`; ask counts
come from `tally_state`. A node is **probeable** when its status is not
`understood` and `times_asked < MAX_ASKS_PER_NODE`. Graded probeable nodes are
listed first (`ordered_nodes` = graded, then ungraded, stable within each group);
`reserved_for_graded` is how many of them remain. `contested_ids` (P3.2 L2a,
level >= 2) then stable-partitions the graded block so contested nodes lead it.
When `cap - questions_asked <= reserved_for_graded` the policy flips `graded_only` and
`askable_ids` drops every ungraded node — the last questions of a session are
held for territory the grade is actually computed over.

## Invariants & gotchas

- **`contested_ids` reorders, it never admits.** A node the questioning engine
  labelled with a wrongness is ALREADY askable (it is not `understood`); the
  signal only buys it priority. `MAX_ASKS_PER_NODE` stays 2 — a contradiction
  consumes the second ask, it never creates a third (P2.4 / #191 confirm-once) —
  and `reserved_for_graded` / `graded_only` / `graded_ids` are unmoved. An empty
  `contested_ids` returns the untouched list, so levels 0/1 cannot differ from
  the pre-P3.2 policy even structurally.
- **One definition of "graded".** `GRADED_NODE_TYPES` is imported from
  `overseer/topic_score::_GRADED_NODE_TYPES` — the grader's set — never re-declared
  here. Adding a graded node type there changes questioning automatically.
- **No lockout on a 0-graded problem.** With no graded nodes `reserved_for_graded`
  is 0, `graded_only` stays False and ungraded nodes stay askable.
- **Empty `askable_ids` is the done condition** (`unified` forces `done` on it) —
  not an error state. It is reachable only when every node is `understood` or out
  of asks, and there is no legal target in it, so `askable_ids[0]` is safe
  everywhere else.
- **`open_graded_topics` ≠ "topics Apollo will still ask about".** `open_graded_ids`
  filters on `status != 'understood'` ONLY — askability is deliberately not applied,
  because the cross-repo contract pins the count as "graded nodes not yet
  understood". A graded node capped at `MAX_ASKS_PER_NODE` while still `tentative`
  therefore counts forever and cannot be cleared by talking longer. UI copy built on
  this number must say "not yet marked understood", never "Apollo will ask about
  these next".
- The policy is pure: no DB, no LLM, no I/O. Both callers compute it twice per
  turn (before the call for the payload, after the call with this turn's updates
  for enforcement) and the two results may legitimately differ.

## Related

Engine `questioning/unified` (enforces the policy); persistence
`questioning/controller` (serves the counts, derives `contested_ids`); done-gate
`questioning/challenge`; graded-type authority `overseer/topic-score`; `KGGraph`
`ontology/graph`.
