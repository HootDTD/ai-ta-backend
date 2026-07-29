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
last_verified: 2026-07-25
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
- `__all__ = ["cheap_chat", "main_chat"]`.

## Data flow

Each tier builds a kwargs dict (`model`/`messages`/`temperature`, plus optional
`response_format`), calls `_client().chat.completions.create(**kwargs)`, emits a
structured `llm_call` log line via `_log_call` (`purpose`/`model`/`tokens_in`/
`tokens_out` read from `response.usage`), and returns `choices[0].message.content or ""`.

## Invariants & gotchas

- Model resolution is env-driven with a hardcoded fallback — **never a model
  literal at call sites**. `purpose=` is a grep-friendly audit tag.
- `_client()` returns a **fresh `OpenAI()` per call** — there is no process
  singleton (the Neo4j client is the singleton; this is not).
- LIVE consumers of `cheap_chat`: `handlers/intent` (intent classifier) and
  `parser/parser_llm` (`_classify_teaching` triviality gate). It is also imported
  by the vestigial `agent/leakage_judge` and `handlers/history` (dead paths).
- **DRIFT: `main_chat` currently has NO live importer.** The module docstring
  claims it backs "parser, draft reply, coverage matcher", but the parser uses
  its own `OpenAI()` client at MAIN_MODEL, `draft_reply` is vestigial, and the
  Done-time transcript audit (`overseer/transcript_coverage`) also builds a direct
  `OpenAI()` client — none call `main_chat`. `provisioning/metered_chat` provides
  its own `cheap_chat`/`main_chat`-**shaped** metered tiers (it does not import
  these). So `main_chat` is a defined-but-uncalled tier today.

## Env flags

- `APOLLO_CHEAP_MODEL` — cheap-tier model override (default `gpt-4o-mini`).
- The main tier is backed by the `config.models.MAIN_MODEL` code pin
  (`platform/config-model-pins`), not an env flag. (`APOLLO_MODEL` is read by the
  vestigial `agent/apollo_llm`, not here.)

## Related

Callers `handlers/intent`, `parser/parser-llm`; metered wrapper
`provisioning/metered-chat`; model pin `platform/config-model-pins`; dead
consumers `agent/persona-reply`, `agent/output-filter`.
