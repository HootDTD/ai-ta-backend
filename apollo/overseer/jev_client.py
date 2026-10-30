"""Async HTTP client for TypeSafe Jev (``POST /v1/systemone``), the decision
model behind the experimental Apollo node judge (``jev_node_judge.py``).

Jev cannot generate text: every question is a ``choice`` it answers with one
label plus a probability per label. This module only speaks the wire protocol —
prompt wording and aggregation live in ``jev_node_judge``.

Retry policy (spec 2026-10-29 §1 Wiring): 429 / 529 retry with a 1s / 2s / 4s
backoff plus jitter; 401 / 422 (and every other non-2xx) raise at once; a
timeout raises at once. Every failure surfaces as :class:`JevError`, which the
Done path catches to fall back to the GPT adjudicator. The API key is read from
``JEV_API_KEY`` and is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import get_jev_api_key

_LOG = logging.getLogger(__name__)

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 15.0
# Base delays before retry 1, 2, 3 of a 429/529. Each is stretched by up to
# ``_JITTER_FRACTION`` so concurrent turns that hit the limit together spread out.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
_JITTER_FRACTION = 0.25
_RETRY_STATUSES = frozenset({429, 529})


class JevError(Exception):
    """Any Jev failure: transport, timeout, non-2xx, or a malformed body."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class JevAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class JevResponse:
    model: str
    answers: dict[str, JevAnswer]
    usage: dict[str, int]
    # Wall time of the whole ``evaluate`` call, retries and backoff included —
    # the latency a student turn actually pays.
    latency_ms: float


def _parse_answer(qid: str, raw: object) -> JevAnswer:
    if not isinstance(raw, Mapping):
        raise JevError(f"jev answer {qid!r} is not an object")
    choice = raw.get("choice")
    confidence = raw.get("confidence")
    probabilities = raw.get("probabilities")
    if not isinstance(choice, str):
        raise JevError(f"jev answer {qid!r} has no choice")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise JevError(f"jev answer {qid!r} has no numeric confidence")
    if not isinstance(probabilities, Mapping):
        raise JevError(f"jev answer {qid!r} has no probabilities")
    return JevAnswer(
        choice=choice,
        confidence=float(confidence),
        probabilities={str(label): float(p) for label, p in probabilities.items()},
    )


def _parse_body(body: object, *, latency_ms: float) -> JevResponse:
    if not isinstance(body, Mapping) or not isinstance(body.get("answers"), Mapping):
        raise JevError("jev response has no answers object")
    raw_usage = body.get("usage") or {}
    usage = {
        str(key): int(value)
        for key, value in (raw_usage.items() if isinstance(raw_usage, Mapping) else ())
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return JevResponse(
        model=str(body.get("model", "")),
        answers={str(qid): _parse_answer(str(qid), raw) for qid, raw in body["answers"].items()},
        usage=usage,
        latency_ms=latency_ms,
    )


class JevClient:
    """One instance may serve many concurrent ``evaluate`` calls.

    ``transport`` is injectable (an ``httpx.MockTransport`` in tests), as are
    ``sleep`` and ``jitter`` so the backoff schedule is observable without
    waiting on it."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = JEV_ENDPOINT,
        model: str = JEV_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._api_key = api_key if api_key is not None else get_jev_api_key()
        self.endpoint = endpoint
        self.model = model
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep
        self._jitter = jitter

    def build_body(self, state: object, questions: Mapping[str, Any]) -> dict[str, Any]:
        return {"model": self.model, "state": state, "questions": dict(questions)}

    async def evaluate(self, state: object, questions: Mapping[str, Any]) -> JevResponse:
        """Ask every question in ``questions`` about ``state`` in ONE request."""
        if not self._api_key:
            raise JevError("JEV_API_KEY is not set")
        body = self.build_body(state, questions)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        started = time.perf_counter()
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
            attempt = 0
            while True:
                try:
                    response = await http.post(self.endpoint, json=body, headers=headers)
                except httpx.TimeoutException as exc:
                    raise JevError(f"jev request timed out: {type(exc).__name__}") from exc
                except httpx.HTTPError as exc:
                    raise JevError(f"jev transport error: {type(exc).__name__}") from exc
                status = response.status_code
                if status in _RETRY_STATUSES and attempt < len(RETRY_BACKOFF_SECONDS):
                    delay = RETRY_BACKOFF_SECONDS[attempt] * (
                        1.0 + _JITTER_FRACTION * self._jitter()
                    )
                    _LOG.warning(
                        "jev_retry status=%s attempt=%d delay_s=%.2f", status, attempt + 1, delay
                    )
                    await self._sleep(delay)
                    attempt += 1
                    continue
                if status != 200:
                    raise JevError(f"jev returned HTTP {status}", status_code=status)
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise JevError("jev response is not JSON", status_code=status) from exc
                latency_ms = (time.perf_counter() - started) * 1000.0
                return _parse_body(payload, latency_ms=latency_ms)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "JEV_ENDPOINT",
    "JEV_MODEL",
    "RETRY_BACKOFF_SECONDS",
    "JevAnswer",
    "JevClient",
    "JevError",
    "JevResponse",
]
