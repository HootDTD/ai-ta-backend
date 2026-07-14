"""WU-3B2f — the per-call metered LLM client (the ONLY token signal in §8B).

``apollo/agent/_llm.py`` reads ``response.usage`` solely to emit a log line and
then DISCARDS it (it returns only ``str``), so there is no programmatic token
count anywhere in the pipeline. ``MeteredChat`` re-invokes the OpenAI client
ITSELF (it does NOT import or call ``_llm`` — that would couple every existing
caller and still discard usage), captures ``response.usage.prompt_tokens`` /
``completion_tokens``, accumulates ``llm_calls`` / ``llm_tokens_in`` /
``llm_tokens_out`` / ``llm_cost_usd`` onto the passed ``apollo_ingest_runs`` row,
and after each call compares the running cumulative (in+out) token total against
``PER_DOCUMENT_TOKEN_CEILING``; on breach it raises ``CostBudgetExceeded`` — the
abort signal the orchestrator (WU-3B2g) turns into an ``apollo_ingest_errors``
row + a failed run.

The ingest_run row is MUTATED IN PLACE via SQLAlchemy attribute assignment: the
row is the durable per-document aggregate, so this is the INTENDED ORM write
(flushed/committed by the orchestrator's session), NOT a value-object mutation.
The pure value object here (``CostBudgetExceeded``) carries only counts/ids.

Model routing mirrors ``_llm`` EXACTLY (``APOLLO_CHEAP_MODEL`` default
``gpt-4o-mini`` / ``MAIN_MODEL`` default ``gpt-4o``; an explicit ``model=`` arg
overrides). ``cheap`` / ``main`` accept the SAME keyword shape as
``_llm.cheap_chat`` / ``main_chat`` so a stage written against the injected
``chat_fn`` can be handed ``metered.cheap`` / ``metered.main`` unchanged;
``scrape_chat_fn`` covers the one positional-string seam (``scrape.py:141``).

Security: the client is built via ``openai.OpenAI()`` (key read from
``OPENAI_API_KEY`` by the SDK); no key is ever an argument or logged. The
structured logs carry purpose / model / token-COUNTS / ids only — never prompt
or completion bodies, never PII.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from apollo.provisioning.cost_constants import (
    PER_DOCUMENT_TOKEN_CEILING,
    cost_usd_for,
)

_LOG = logging.getLogger(__name__)

_CHEAP_MODEL_DEFAULT = "gpt-4o-mini"
_MAIN_MODEL_DEFAULT = "gpt-4o"


class CostBudgetExceeded(Exception):
    """Raised when a metered run's cumulative tokens cross the per-document
    ceiling. Carries the cumulative ``tokens``, the ``ceiling``, and the
    ``document_id`` (when known) — never prompt content or the API key."""

    def __init__(self, *, tokens: int, ceiling: int, document_id: int | None = None) -> None:
        self.tokens = tokens
        self.ceiling = ceiling
        self.document_id = document_id
        super().__init__(
            f"per-document token ceiling exceeded: {tokens} > {ceiling} (document_id={document_id})"
        )


def _is_reasoning_model(model: str) -> bool:
    """True if the model takes reasoning_effort (mirrors ai/main_ai.py:923)."""
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _resolve_model(env_var: str, fallback: str) -> str:
    return os.getenv(env_var) or fallback


def _make_default_client():  # pragma: no cover - exercised only when no client injected
    from openai import OpenAI

    return OpenAI()


class MeteredChat:
    """Wraps the OpenAI client to capture ``response.usage`` and accumulate
    token/cost onto a passed ``apollo_ingest_runs`` row, raising
    ``CostBudgetExceeded`` at the per-document ceiling."""

    def __init__(
        self,
        *,
        ingest_run: Any,
        client: Any | None = None,
        ceiling: int = PER_DOCUMENT_TOKEN_CEILING,
        document_id: int | None = None,
    ) -> None:
        self._run = ingest_run
        self._client = client if client is not None else _make_default_client()
        self._ceiling = ceiling
        self._document_id = document_id

    # -- public tiers (drop-in for the injected cheap_chat/main_chat chat_fn) -- #
    def cheap(
        self,
        *,
        purpose: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.0,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Cheap-tier (gpt-4o-mini default) metered call. cheap_chat-shaped."""
        used_model = model or _resolve_model("APOLLO_CHEAP_MODEL", _CHEAP_MODEL_DEFAULT)
        return self._call(
            purpose=purpose,
            model=used_model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )

    def main(
        self,
        *,
        purpose: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.0,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Main-tier (gpt-4o default) metered call. main_chat-shaped."""
        used_model = model or _resolve_model("MAIN_MODEL", _MAIN_MODEL_DEFAULT)
        return self._call(
            purpose=purpose,
            model=used_model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )

    def scrape_chat_fn(self, system_prompt: str) -> Callable[[str], str]:
        """Adapter for the positional-string ``chat_fn`` seam (``scrape.py:141``):
        ``chat_fn(chunk_content) -> str``, routed cheap, metered."""

        def _chat_fn(chunk_content: str) -> str:
            return self.cheap(
                purpose="scrape",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": chunk_content},
                ],
            )

        return _chat_fn

    def cumulative_tokens(self) -> int:
        """Return the read-only cumulative input+output token count.

        Stages use snapshots of this aggregate to account for their own spend;
        the ingest-run row remains the single mutable ledger.
        """
        return int(self._run.llm_tokens_in or 0) + int(self._run.llm_tokens_out or 0)

    # -- internals -- #
    def _call(
        self,
        *,
        purpose: str,
        model: str,
        messages: list[dict],
        response_format: dict | None,
        temperature: float,
        reasoning_effort: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if reasoning_effort is not None and _is_reasoning_model(model):
            # Reasoning models (gpt-5.x / o-series) take reasoning_effort and
            # reject temperature (same convention as ai/main_ai.py:1369). A
            # non-reasoning model (e.g. the gpt-4o default) silently ignores
            # the requested effort and keeps temperature — callers pass effort
            # opportunistically, not as a hard requirement.
            kwargs["reasoning_effort"] = reasoning_effort
        else:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format
        resp = self._client.chat.completions.create(**kwargs)
        self.record_usage(model=model, usage=getattr(resp, "usage", None))
        _LOG.info(
            "llm_call",
            extra={
                "event": "llm_call",
                "purpose": purpose,
                "model": model,
                "tokens_in": getattr(getattr(resp, "usage", None), "prompt_tokens", 0),
                "tokens_out": getattr(getattr(resp, "usage", None), "completion_tokens", 0),
            },
        )
        return resp.choices[0].message.content or ""

    def record_usage(self, *, model: str, usage: Any) -> None:
        """Accumulate ONE call's usage onto the ingest_run row (``+=``, never
        overwrite), then raise ``CostBudgetExceeded`` if the cumulative (in+out)
        token total now exceeds the ceiling. The breaching call's counts ARE
        recorded before the raise (so the run reflects the spend that aborted)."""
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)

        self._run.llm_calls += 1
        self._run.llm_tokens_in += tokens_in
        self._run.llm_tokens_out += tokens_out
        self._run.llm_cost_usd += cost_usd_for(model, tokens_in=tokens_in, tokens_out=tokens_out)

        cumulative = self._run.llm_tokens_in + self._run.llm_tokens_out
        if cumulative > self._ceiling:
            _LOG.warning(
                "provisioning_cost_abort",
                extra={
                    "event": "provisioning_cost_abort",
                    "document_id": self._document_id,
                    "ingest_run_id": getattr(self._run, "id", None),
                    "tokens": cumulative,
                    "ceiling": self._ceiling,
                },
            )
            raise CostBudgetExceeded(
                tokens=cumulative,
                ceiling=self._ceiling,
                document_id=self._document_id,
            )


__all__ = ["MeteredChat", "CostBudgetExceeded"]
