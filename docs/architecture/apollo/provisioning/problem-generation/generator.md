---
doc: ai-ta-backend/apollo/provisioning/problem-generation/generator
description: Core variant generator — apply operators to an approved seed, solution-check, leak-guard, and persist held tier-1 variants
owns:
  - apollo/provisioning/problem_generation/generator.py
related:
  - ai-ta-backend/apollo/provisioning/problem-generation/_index
  - ai-ta-backend/apollo/provisioning/problem-generation/support
  - ai-ta-backend/apollo/provisioning/problem-generation/api
  - ai-ta-backend/apollo/provisioning/solution
  - ai-ta-backend/apollo/provisioning/problem-leak-guard
  - ai-ta-backend/apollo/provisioning/authored-sets/verification
  - ai-ta-backend/apollo/provisioning/metered-chat
  - ai-ta-backend/apollo/schemas/problem
  - ai-ta-backend/platform/config-model-pins
last_verified: 2026-07-25
stub: false
---

## Interface

- `generate_problem_variants(db, *, concept_id, seed_problem_ids, count,
  metered_chat, search_space_id, retrieve_fn=None)` → `GenerationRunResult` — the
  single entry (called by `problem_generation/api._run_generation_background`).
- `GenerationRunResult`, `GenerationRecord`, `GeneratedCandidate`,
  `ProblemGenerationDisabled`, `problem_generation_enabled`,
  `generation_token_ceiling`, `generation_max_variants`.

## Data flow

Validate the requested tier-2 seeds (course + concept + not-quarantined). For each
`(seed, applicable operator)` assignment: build the variant via `metered_chat.main`,
parse + reject on `invalid_variant`/`duplicate` (normalized-statement exact-dedup),
build a reference via `solution.find_or_generate` (using
`verification._empty_retrieve` for no-grounding), `round_trip_check`, leak-guard via
`problem_leak_guard.check_problem_leak`, then optional `qualitative_rubric` for prose
seeds. Survivors persist as tier-1 `generated` rows held for review; the run flushes
once and back-fills each record's `concept_problem_id`.

## Invariants & gotchas

- **Fail-closed:** raises `ProblemGenerationDisabled` unless `APOLLO_PROBLEM_GENERATION`
  is set; a `CostBudgetExceeded` from the metered client breaks the loop with a
  `budget_exceeded` drop (partial results still persist).
- Drops are typed: `leaked`, `duplicate`, `solution_failed`, `invalid_seed`,
  `invalid_variant`, `budget_exceeded`, `refuted`.
- The leak judge is advisory/fail-open, but the metering circuit-break is made
  visible: a `CostBudgetExceeded` raised inside the leak chat is re-raised at run level.
- A `refuted` round-trip verdict drops the variant; `inapplicable` (prose) routes to
  the qualitative rubric instead. Variants keep the seed's difficulty.

## Env flags

`APOLLO_PROBLEM_GENERATION`, `APOLLO_PROBLEM_GENERATION_TOKEN_CEILING` (default 200k),
`APOLLO_PROBLEM_GENERATION_MAX_VARIANTS` (default 10). Read per call.

## Related

`operators.VARIATION_OPERATORS`, `verifiers.round_trip_check`/`qualitative_rubric`
(support), `solution.find_or_generate`/`SolutionDraftError`,
`problem_leak_guard.check_problem_leak`, `schemas.problem.Problem`,
`config.models.MAIN_MODEL` (platform/config-model-pins).
