"""Shared OpenAI client helpers for Apollo agent code.

Two budget tiers:
- `cheap_chat` — for cross-checks (filter judge, intent classifier, parser
  triviality, history summarizer). Uses `APOLLO_CHEAP_MODEL` env var with
  a `gpt-4o-mini` default.
- `main_chat` — for reasoning calls (parser, draft reply, coverage matcher).
  Uses the pinned `config.models.MAIN_MODEL` (a per-call `model=` arg overrides).

Both emit a structured log line per call so cost can be audited after a
session. Callers pass `purpose=` so the audit log is grep-friendly.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from openai import OpenAI

from config import models

_LOG = logging.getLogger(__name__)

_CHEAP_MODEL_DEFAULT = "gpt-4o-mini"

_TIMEOUT_ENV = "APOLLO_OPENAI_TIMEOUT_S"
_TIMEOUT_DEFAULT_S = 90.0
_MAX_RETRIES_ENV = "APOLLO_OPENAI_MAX_RETRIES"
_MAX_RETRIES_DEFAULT = 1


def bounded_client() -> OpenAI:
    """OpenAI client with an explicit request timeout and retry cap.

    The SDK defaults (600 s timeout, 2 retries) let one hung request freeze
    an Apollo turn for many minutes. Every Apollo OpenAI client is built
    here so the tail stays bounded and tunable per environment. A per-call
    ``timeout=`` still overrides the client-level value.
    """
    return OpenAI(
        timeout=float(os.getenv(_TIMEOUT_ENV) or _TIMEOUT_DEFAULT_S),
        max_retries=int(os.getenv(_MAX_RETRIES_ENV) or _MAX_RETRIES_DEFAULT),
    )


def _client() -> OpenAI:
    return bounded_client()


def _resolve_model(env_var: str, fallback: str) -> str:
    return os.getenv(env_var) or fallback


def _log_call(*, purpose: str, model: str, response: Any) -> None:
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
    tokens_out = getattr(usage, "completion_tokens", None) if usage else None
    _LOG.info(
        "llm_call",
        extra={
            "event": "llm_call",
            "purpose": purpose,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        },
    )


def cheap_chat(
    *,
    purpose: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.0,
    model: str | None = None,
) -> str:
    """Cheap-tier chat call. Returns the assistant message content (string).

    `purpose` is a short audit tag (e.g. "leakage_judge", "intent_classifier").
    """
    used_model = model or _resolve_model("APOLLO_CHEAP_MODEL", _CHEAP_MODEL_DEFAULT)
    kwargs: dict[str, Any] = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    resp = _client().chat.completions.create(**kwargs)
    _log_call(purpose=purpose, model=used_model, response=resp)
    return resp.choices[0].message.content or ""


def main_chat(
    *,
    purpose: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.0,
    model: str | None = None,
) -> str:
    """Main-tier chat call. Returns the assistant message content (string)."""
    used_model = model or models.MAIN_MODEL
    kwargs: dict[str, Any] = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    resp = _client().chat.completions.create(**kwargs)
    _log_call(purpose=purpose, model=used_model, response=resp)
    return resp.choices[0].message.content or ""


__all__ = ["bounded_client", "cheap_chat", "main_chat"]
