---
doc: campaign/judges-s3-s4
description: The two live teaching-loop transcript-quality judges — student-graph fidelity (resolver recall) and Apollo coherence.
owns:
  - campaign/judges/s3_student_fidelity.py
  - campaign/judges/s4_apollo_coherence.py
related:
  - campaign/judges-base
  - campaign/cast-student
  - campaign/cast-personas
last_verified: 2026-07-25
stub: false
---

# campaign/judges-s3-s4 — student fidelity + Apollo coherence

Two `StageJudge` subclasses consuming the attempt JSONL from
[cast-student](cast-student.md)`.append_attempt_record`.

## Interface

- **S3 (`s3_student_fidelity.py`)** `S3StudentFidelityJudge` — THE resolver
  recall/precision audit. One item per node-ledger entry: for
  `credited`/`misconception`, does the cited `evidence_span` really show the
  student teaching (phantom-credit check); for `unresolved`, does the FULL
  transcript teach a concept the resolver missed (missed-credit / recall gap).
  SEPARATELY, the pure-code `ledger_vs_expected(ledger, expected)` diffs the
  actual ledger against the persona's authored `ExpectedLedger` and reports
  agreement via `JudgeResult.extra` — **NOT folded into the LLM `pass_rate`**
  (it audits "did it land where the author predicted", a different question).
  Gate (E3): ≥95% item-level.
- **S4 (`s4_apollo_coherence.py`)** `S4ApolloCoherenceJudge` — one item per
  sampled session. LLM-checks that Apollo's questions targeted concepts that
  ended up unresolved/misconceived (not ones already taught cleanly) AND that
  grading honored clarification resolutions (a credited clarification must appear
  as a credited/clarification ledger entry, not a lingering unresolved one).
  Gate (E3): coherent on ≥90% of sampled sessions (session-level bar).

## Invariants & gotchas

- S3 audits only `credited`/`misconception`/`unresolved` statuses; other
  statuses are skipped (not the recall/precision question).
- `ledger_vs_expected` is a hard code-side diff, never an LLM call.

## Related

- [judges-base](judges-base.md), [cast-student](cast-student.md) (attempt JSONL),
  [cast-personas](cast-personas.md) (`ExpectedLedger`).
