---
doc: apollo/conversation/routing/errors
description: apollo/errors.py — the apollo-wide NO-FALLBACK named-exception taxonomy, grouped by HTTP status
owns:
  - apollo/errors.py
related:
  - apollo/conversation/routing/router
last_verified: 2026-07-25
stub: false
---

# routing/errors — the NO-FALLBACK exception taxonomy

`apollo/errors.py` is the data layer of the NO-FALLBACK contract: every failure
mode is a typed `ApolloError` subclass carrying structured fields, surfaced as
JSON by `routing/router`'s `register_exception_handlers`. This file is a
**shared apollo-wide reference** — grading, KG, and provisioning code raise
these — so other apollo docs `related:`-link here rather than redefining
semantics.

## Interface

Base `ApolloError(Exception)` + ~17 subclasses. Each carries the fields the
handler lifts into the JSON body. Grouped by the HTTP status `router` maps them to:

- **422 input/parse** — `ParserCouldNotExtractError(utterance)`, `FilterRejectedError(rejected_term, draft, kg)`, `MalformedEquationError(entry_id, symbolic, parse_error)`.
- **409 conflict/state** — `NoMatchingConceptError(transcript_summary)`, `PoolExhaustedError(concept_cluster_id, difficulty)`, `SessionFrozenError(session_id)`, `InvalidPhaseError(session_id, phase)`.
- **404 not-found** — `KGEntryNotFoundError(attempt_id, node_id)`, `ProblemNotFoundError(problem_id, concept_id)`.
- **503 degraded/infra** — `KGUnavailableError(stage, last_error)`, `CoverageGradingError(stage, last_error)`, `ResolutionUnavailableError(stage, last_error)`, `TranscriptAuditUnavailableError(last_error)`.
- **500** — `ResolutionInvalidOutputError(returned_key, allowed_keys)` (closed-set hallucination; payload carries only the *count* of allowed keys).
- **Named but NOT HTTP-registered** — `CanonProjectionError`, `RetentionError`, `LearnerUpdateUnreconstructableError(attempt_id, reason)` + its closed `LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS` tuple.

## Invariants & gotchas

- **Grade-committed errors must never void an earned grade.** `RetentionError`,
  `ResolutionUnavailableError`, and `TranscriptAuditUnavailableError` all raise
  *after* the student's grade + XP are already durable; they surface loudly (NO
  FALLBACK) but the next Done / retry / janitor re-runs idempotently. The
  user-facing 503 message reads "your grade is saved" for exactly these.
- **`KGUnavailableError` is degraded-mode, not the loud-crash family**: Neo4j is
  optional for the student interaction, so this is infra optionality surfaced as
  a structured 503 rather than a false F.
- **`TranscriptAuditUnavailableError`**: a valid `{"spans": {}}` reply ("student
  taught none") does NOT raise — only a transient/parse failure does; the
  orchestrator converts it into a suppress-all-`missing` abstention.
- `ResolutionInvalidOutputError` bounds its payload — allowed-key *count*, never
  the full candidate list (audit hygiene).

## Related

`routing/router` registers these classes as JSON handlers; grading / KG /
provisioning leaves across the apollo tree raise them and link back here.
