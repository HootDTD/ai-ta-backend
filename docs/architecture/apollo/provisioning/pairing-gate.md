---
doc: apollo/provisioning/pairing-gate
description: Stage-3 two-phase span-grounded correctness gate deciding whether a reference solution is teachable.
owns:
  - apollo/provisioning/pairing_gate.py
related:
  - apollo/provisioning/solution
  - apollo/provisioning/provisioning-schema
last_verified: 2026-07-25
stub: false
---

# provisioning/pairing-gate

Stage-3: `validate_pair` decides whether a `ReferenceSolutionDraft` may become teachable
for a `CandidateQuestion`, via a two-phase span-grounded LLM judge over an injected
`judge_fn`. Phase A checks pairing/answer-relevance (short-circuits Phase B when unpaired);
Phase B is claim-decomposed faithfulness (any unentailed claim ⇒ `faithful=False`).

## Interface

- `validate_pair(question, draft, *, retrieve_fn, judge_fn) -> PairingVerdict` — the gate.
- `rejection_from_verdict(verdict) -> Rejection | None` — the single fail-mapping point (None on approve).
- `PairingVerdict` — `paired` / `faithful` / `failed_claims` / `confidence`; `.approved` == paired AND faithful.
- `Rejection` — the typed FAIL handoff (`reason` ∈ unparseable_judge / no_claims_decomposed / not_paired / unfaithful_claims).

All four are re-exported by the package facade (`provisioning/_index`).

## Data flow

The judge sees the SAME grounding the generator used — `draft.grounding` when present, else
a re-retrieve via `retrieve_fn(question)`. Phase A and Phase B each go through one private
helper, `_judge_or_fail_closed`, which calls `judge_fn`, parses the JSON, and returns the
parsed dict or `None`. Both phases use the strict `json_schema`s from `provisioning_schema`.
A FAIL verdict is mapped to a typed `Rejection` by `rejection_from_verdict`; callers retain
the outcome in their result ledger.

## Invariants & gotchas

- **FAIL-CLOSED is the load-bearing property** — the EXPLICIT INVERSION of the §6 leakage
  judge (which fails OPEN). Any malformed / non-JSON / exception response at EITHER phase, or
  a degenerate Phase-B that decomposes ZERO claims, yields a REJECT
  (`paired`/`faithful` false, `confidence=0`). Routing every `judge_fn` call through
  `_judge_or_fail_closed` guarantees a future third judge call cannot skip the default —
  provisioning prefers a false-reject to a false-approve.
- **Same §1.8 / OPS-6 caveat as `solution`** — a coherent-but-wrong solution can still pass
  and be shown in shadow; the gate reduces, not eliminates, that case.
- **Logs carry only counts / booleans / `solution_source`** — never solution/question/passage
  text or PII. No DB write; `judge_fn`/`retrieve_fn` are injected (inputs are course material only).

## Related

- `provisioning/solution` — produces the `ReferenceSolutionDraft` + `GroundingSpan`s this gate judges.
- `provisioning/provisioning-schema` — `build_pairing_phase_a_schema` / `build_pairing_phase_b_schema`.
