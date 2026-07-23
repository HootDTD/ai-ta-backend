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


def _client() -> OpenAI:
    return OpenAI()


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


__all__ = ["cheap_chat", "main_chat"]
