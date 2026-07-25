---
doc: ai-ta-backend/apollo/provisioning/problem-generation/_index
description: Router for the problem_generation subpackage — default-OFF, teacher-initiated generation of held tier-1 problem variants
owns:
  - apollo/provisioning/problem_generation/__init__.py
related: []
last_verified: 2026-07-25
stub: false
---

**Parent router:** [`provisioning/_index`](../_index.md). This is a nested
sub-router — the sanctioned 5–6-hop exception (PLAN R4): `CLAUDE.md → shared README
→ apollo/_index → provisioning/_index → problem-generation/_index → leaf`, justified
because a flat provisioning index would blow the 60-line cap.

Teacher-initiated generation of held tier-1 problem VARIANTS from an approved seed,
gated OFF by default. The package `__init__.py` is the public surface, re-exporting
`generate_problem_variants`, `GenerationRunResult`, `ProblemGenerationDisabled`,
`VARIATION_OPERATORS`, `round_trip_check`, `qualitative_rubric`,
`RoundTripVerdict`/`RubricClaim`/`RubricReport`, and the
`problem_generation_enabled`/`generation_token_ceiling`/`generation_max_variants`
config readers.

## Leaf docs

| Leaf | Owns | One-liner |
|---|---|---|
| [api](api.md) | `problem_generation/api.py` | Teacher batch API (start/list/get/approve); `router` mounts in `apollo/api.py`; reuses `authored_sets.approve_held_row` |
| [generator](generator.md) | `problem_generation/generator.py` | Core variant generator: operator → solution-check → leak-guard → held tier-1 write |
| [support](support.md) | `problem_generation/operators.py`, `verifiers.py` | Variation-operator catalog + round-trip/qualitative verifiers |

## Cross-cutting invariants

- The GEN-1 answer-leak guard the generator invokes lives one level up:
  [`../problem-leak-guard.md`](../problem-leak-guard.md) (physically in the
  provisioning root, conceptually part of this subpackage).
- **Flags:** `APOLLO_PROBLEM_GENERATION` (default off — the generator is fail-closed
  without it), `APOLLO_PROBLEM_GENERATION_TOKEN_CEILING` (default 200k),
  `APOLLO_PROBLEM_GENERATION_MAX_VARIANTS` (default 10). Read per call.
- Generated problems are always held tier-1 for teacher review, then promoted to
  tier-2 via the shared `authored_sets.approve_held_row` savepoint.
