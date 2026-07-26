---
doc: apollo/conversation/agent/output-filter
description: Vestigial two-stage structural-leakage barrier (deterministic pre-filter + LLM judge).
owns:
  - apollo/agent/output_filter.py
  - apollo/agent/leakage_judge.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/agent/persona-reply
  - apollo/conversation/agent/llm-client
  - apollo/conversation/routing/errors
last_verified: 2026-07-25
stub: false
---

> **DELETION CANDIDATE (vestigial).** Nothing live imports `validate_or_raise` or
> the judge; their only historical caller — `draft_reply` (`agent/persona-reply`)
> — is itself dead. The live leakage control is now `questioning/unified`'s
> **log-only "belt"** telemetry.

Two files that formed the V3 two-stage output-leakage barrier.

## Interface

- `output_filter.validate_or_raise(draft, *, concept, history, kg_summary, judge=None, sufficiency=None, misconception=None) -> str`
  — **dead** (raised `FilterRejectedError`). `__all__ = ["validate_or_raise"]`.
- `leakage_judge.llm_leakage_judge(...) -> JudgeVerdict`; `JudgeVerdict` dataclass;
  `LeakageJudge` Protocol (DI seam for tests); `CONFIDENCE_THRESHOLD = 0.6`.

## Data flow

Stage 1 (deterministic): `_pre_filter_offender` scans the draft against
`concept.forbidden_named_laws.all_terms()`, clearing tokens in the student's
vocabulary (`_student_vocabulary`) or introduced named laws (`_introduced_laws` +
`_fuzzy_match` for spelling slips; `_depossess` for `'s`). Plus `_misconception_leak`
(blocks verbatim `description`/`bank_code`/`bank_id` leaks) and `_check_sufficiency_alignment`
(warn-only). Stage 2 (semantic): `llm_leakage_judge` runs the cheap tier
(`agent/llm-client.cheap_chat`), acting only when `leaks` and `confidence >= 0.6`;
soft-fails **open** on a JSON parse error.

## Invariants & gotchas

- The barrier RAISED on a hit; the live belt only records `belt_hit_served`
  telemetry and never rewrites a valid reply — the semantics inverted when the
  reply path moved to `questioning/unified`.
- `provisioning/pairing_gate` references `leakage_judge` only in prose/docstrings
  (as the fail-open contract to invert), **not** as an import.
- `apollo/agent/LEAKAGE_POLICY.md` (prose, not `.py`, outside the bijection) is the
  contract this file enforced — the judge prompt cited it verbatim.

## Env flags

- None directly owned. The dead judge ran on the cheap tier
  (`APOLLO_CHEAP_MODEL`, via `agent/llm-client`).

## Related

Live replacement `questioning/unified`; dead caller `agent/persona-reply`; cheap
tier `agent/llm-client`; `FilterRejectedError` lives in `routing/errors`.
