---
doc: apollo/conversation/agent/llm-client
description: Shared two-tier OpenAI chat helpers (cheap_chat / main_chat) for Apollo agent code.
owns:
  - apollo/agent/_llm.py
  - apollo/agent/__init__.py
related:
  - apollo/conversation/handlers/intent
  - apollo/conversation/parser/parser-llm
  - apollo/conversation/agent/persona-reply
  - apollo/conversation/agent/output-filter
  - apollo/provisioning/metered-chat
  - platform/config-model-pins
last_verified: 2026-08-04
stub: false
---

`apollo/agent/_llm.py` is the shared OpenAI client seam with two budget tiers.
It is a **cross-domain** module — not conversation-private (`apollo/agent/__init__.py`
is empty namespace glue that rides here per D4).

## Interface

- `cheap_chat(*, purpose, messages, response_format=None, temperature=0.0, model=None) -> str`
  — cheap tier. Model resolves to the `model=` arg, else `APOLLO_CHEAP_MODEL`,
  else the `gpt-4o-mini` fallback (`_resolve_model`). Returns the assistant
  content (`""` if empty).
- `main_chat(*, purpose, messages, response_format=None, temperature=0.0, model=None) -> str`
  — main tier. Model resolves to the `model=` arg, else the `config.models.MAIN_MODEL`
  pin.
- `bounded_client() -> OpenAI` (2026-08-04) — the client factory every Apollo
  `OpenAI()` construction site now goes through (`_client()` here, plus the
  direct-client modules listed below). Sets `timeout=` from `APOLLO_OPENAI_TIMEOUT_S`
  (default 90.0 s) and `max_retries=` from `APOLLO_OPENAI_MAX_RETRIES` (default 1),
  bounding the tail the SDK's own defaults (600 s timeout, 2 retries) would
  otherwise leave open — one hung request no longer freezes a turn for minutes.
  A per-call `timeout=` kwarg still overrides the client-level value.
- `__all__ = ["bounded_client", "cheap_chat", "main_chat"]`.

## Data flow

Each tier builds a kwargs dict (`model`/`messages`/`temperature`, plus optional
`response_format`), calls `_client().chat.completions.create(**kwargs)`, emits a
structured `llm_call` log line via `_log_call` (`purpose`/`model`/`tokens_in`/
`tokens_out` read from `response.usage`), and returns `choices[0].message.content or ""`.

## Invariants & gotchas

- Model resolution is env-driven with a hardcoded fallback — **never a model
  literal at call sites**. `purpose=` is a grep-friendly audit tag.
- `_client()` returns a **fresh `bounded_client()` per call** — there is no
  process singleton (the Neo4j client is the singleton; this is not).
- LIVE consumers of `cheap_chat`: `handlers/intent` (intent classifier) and
  `parser/parser_llm` (`_classify_teaching` triviality gate). It is also imported
  by the vestigial `agent/leakage_judge` and `handlers/history` (dead paths).
- **DRIFT: `main_chat` currently has NO live importer.** The module docstring
  claims it backs "parser, draft reply, coverage matcher", but the parser builds
  its own `bounded_client()` at MAIN_MODEL, `draft_reply` is vestigial, and the
  Done-time transcript audit (`overseer/transcript_coverage`) also builds a direct
  `bounded_client()` — none call `main_chat`. `provisioning/metered_chat` provides
  its own `cheap_chat`/`main_chat`-**shaped** metered tiers (it does not import
  these). So `main_chat` is a defined-but-uncalled tier today. All of those direct
  clients still go through `bounded_client()` (2026-08-04), so the timeout/retry
  cap applies uniformly even though the shared `cheap_chat`/`main_chat` tiers
  aren't the ones calling it.

## Env flags

- `APOLLO_CHEAP_MODEL` — cheap-tier model override (default `gpt-4o-mini`).
- The main tier is backed by the `config.models.MAIN_MODEL` code pin
  (`platform/config-model-pins`), not an env flag. (`APOLLO_MODEL` is read by the
  vestigial `agent/apollo_llm`, not here.)
- `APOLLO_OPENAI_TIMEOUT_S` (default `90.0`) / `APOLLO_OPENAI_MAX_RETRIES`
  (default `1`) — read by `bounded_client()`, applied to every Apollo OpenAI
  client (this module and every direct-client module it seeds).

## Related

Callers `handlers/intent`, `parser/parser-llm`; metered wrapper
`provisioning/metered-chat`; model pin `platform/config-model-pins`; dead
consumers `agent/persona-reply`, `agent/output-filter`.
