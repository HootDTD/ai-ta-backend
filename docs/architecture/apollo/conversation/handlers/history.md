---
doc: apollo/conversation/handlers/history
description: apollo/handlers/history.py — VESTIGIAL windowed-history loader with zero live importers (deletion candidate)
owns:
  - apollo/handlers/history.py
related:
  - apollo/conversation/handlers/chat
  - apollo/conversation/agent/persona-reply
last_verified: 2026-08-04
stub: false
---

# handlers/history — VESTIGIAL

> **DELETION CANDIDATE (D19 / Risk R1).** Zero live importers. Documented only to
> keep the ownership bijection intact; deletion is an owner decision.

`apollo/handlers/history.py` was the bounded chat-history loader (Item #2): the
last `RAW_WINDOW_TURNS` turns verbatim plus a rolling `history_summary` refreshed
via `cheap_chat`. It has been superseded — `handlers/chat` uses its OWN
`_load_history` (full per-attempt transcript, no windowing/summary), and the only
reader of `history_summary` was the now-dead `agent/persona-reply.draft_reply`.

## Interface

None live. Historical public API: `load_windowed_history(*, db, session,
attempt_id) -> (summary_or_none, raw_window)` plus `RAW_WINDOW_TURNS` /
`REFRESH_EVERY_K_TURNS`; internal helpers `_all_messages`, `_format_for_llm`,
`_summarize`.

## Invariants & gotchas

- Dead code still gets swept by mechanical repo-wide passes: `load_windowed_history`'s
  `_summarize` call now runs via `await asyncio.to_thread(_summarize, older)`
  (2026-08-04, part of the Apollo event-loop-offloading sweep) rather than
  inline — inert here since there is no live caller, but keeps the module
  consistent with the live handlers if it is ever revived.
- The `TutoringSession.history_summary` / `history_summary_up_to_turn` columns
  this module wrote are now only ever **reset to `None`** by `handlers/lifecycle`,
  `next.py`, and `restart_problem.py` — never populated by a live writer. Their
  fate should be decided alongside this file's deletion.
- The intended contract (verbatim tail + soft-failing rolling summary) is recorded
  here for the owner; do not treat any of it as the live path.

## Related

Live replacement: `handlers/chat` (`_load_history`); co-dead reader:
`agent/persona-reply`.
