# Apollo smart question planner implementation plan

## Outcome

Replace the endlessly probing, student-KG-driven reply policy with a default-off,
reference-graph-driven controller that asks each unresolved authored node at most once and
automatically enters the existing Done path when no eligible node remains.

## Architecture

1. Evidence evaluator: one structured-output call compares the cumulative student-only
   transcript against every reference node and labels it covered, partial, missing, or
   misconceived. Apollo turns are never evidence.
2. Pure planner: prefer prerequisite-ready unresolved nodes; exclude covered nodes and any
   node already present in the opportunity ledger.
3. Safe writer: receive only the selected private target and student words, not the student
   KG; produce one short question. Reject private target phrases the student did not use.
4. Termination: after one response, close the opportunity regardless of outcome. If every
   node is covered or every remaining gap has been asked, persist a closing turn and invoke
   `handle_done`, returning its existing embedded chat response.

## Persistence and rollout

- Migration 042 adds `apollo_reference_question_opportunities`, unique by attempt and
  reference node, with RLS enabled and no PostgREST policies.
- `APOLLO_SMART_QUESTIONS_ENABLED` defaults off. Migration must land before a staging flip.
- The legacy ambiguity clarification loop and KG-driven drafter remain unchanged when off.

## Verification

- Pure policy: prerequisite ordering, no repeat, all-covered and exhausted-gap stop.
- Evaluator: all node types, unknown/missing verdict fail-closed.
- Writer: target isolation, empty response fallback, private-answer leak fallback.
- Controller/model: state transition and uniqueness contract.
- Handler: question path bypasses `draft_reply`/KG summary; stop embeds Done result.
