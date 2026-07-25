---
doc: ai-ta-backend/apollo/conversation/handlers/olm-invite
description: apollo/handlers/olm_invite.py — VESTIGIAL P3.5 clarification-invite logic, never wired (deletion candidate)
owns:
  - apollo/handlers/olm_invite.py
related:
  - ai-ta-backend/apollo/conversation/handlers/chat
  - ai-ta-backend/apollo/conversation/parser/parser-llm
last_verified: 2026-07-25
stub: false
---

# handlers/olm-invite — VESTIGIAL

> **DELETION CANDIDATE (D19 / Risk R1).** Zero live importers — never wired into
> `handlers/chat` or `routing/router` despite being feature-flagged. Documented
> to keep the bijection intact.

`apollo/handlers/olm_invite.py` is the P3.5 clarification-invite design: when the
parser writes new KG nodes with low `parser_confidence`, Apollo would invite the
student to clarify the wobbliest entry — without naming the wobble — once a
per-session counter and cooldown were met.

## Interface

None live. Historical surface: `is_enabled()` (gated by
`APOLLO_OLM_INVITES_ENABLED`), `decide_invite(...) -> OlmInviteSignal`,
`find_low_conf_new_nodes(nodes, threshold=0.7)`, `signal_to_metadata(signal)`,
the `OlmInviteSignal` dataclass, and cooldown/counter helpers.

## Invariants & gotchas

- The intended trigger contract (low-conf pattern ≥ `COUNTER_THRESHOLD` past
  student turns + expired 60s cooldown, master flag on) is recorded for the owner.
- No live writer ever persists the `olm_invite` message-metadata key it reads, so
  the counter path is inert. Recommend deletion or an explicit wiring decision.

## Env flags

- `APOLLO_OLM_INVITES_ENABLED` — historical master flag; no live read-site
  reaches it because nothing imports this module.

## Related

Intended host: `handlers/chat`; `parser_confidence` source: `parser/parser-llm`.
