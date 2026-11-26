"""TypeSafe Jev client: wire shape, retry policy, and failure surfacing.

Every test runs on an ``httpx.MockTransport`` — no network, no real key."""

from __future__ import annotations

import json

import httpx
import pytest

from apollo.overseer.jev_client import (
    JEV_ENDPOINT,
    JEV_MODEL,
    RETRY_BACKOFF_SECONDS,
    JevClient,
    JevError,
)

pytestmark = pytest.mark.unit

_QUESTIONS = {
    "n0": {
        "type": "choice",
        "instructions": "Judge it.",
        "criteria": {"covered": "a", "partial": "b", "not_covered": "c", "incorrect": "d"},
    }
}
_OK_BODY = {
    "model": "jev-1.14.0",
    "answers": {
        "n0": {
            "type": "choice",
            "choice": "covered",
            "confidence": 0.99,
            "probabilities": {"covered": 0.99, "partial": 0.01},
        }
    },
    "usage": {"input_tokens": 120, "output_tokens": 4},
}


def _client(handler, *, sleeps: list[float] | None = None, **kwargs) -> JevClient:
    recorded = sleeps if sleeps is not None else []

    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    return JevClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep=_sleep,
        jitter=lambda: 0.0,
        **kwargs,
    )


async def test_success_sends_documented_body_and_parses_answers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_OK_BODY)

    response = await _client(handler).evaluate({"student_answer": "x"}, _QUESTIONS)

    request = seen[0]
    assert str(request.url) == JEV_ENDPOINT
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": JEV_MODEL,
        "state": {"student_answer": "x"},
        "questions": _QUESTIONS,
    }
    assert response.model == "jev-1.14.0"
    answer = response.answers["n0"]
    assert answer.choice == "covered"
    assert answer.confidence == pytest.approx(0.99)
    assert answer.probabilities == {"covered": 0.99, "partial": 0.01}
    assert response.usage == {"input_tokens": 120, "output_tokens": 4}
    assert response.latency_ms >= 0.0


@pytest.mark.parametrize("status", [401, 422, 500])
async def test_non_retryable_status_raises_without_retry(status):
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(JevError) as excinfo:
        await _client(handler, sleeps=sleeps).evaluate("state", _QUESTIONS)
    assert excinfo.value.status_code == status
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 529])
async def test_rate_limit_retries_with_backoff_then_succeeds(status):
    responses = [httpx.Response(status), httpx.Response(status), httpx.Response(200, json=_OK_BODY)]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    response = await _client(handler, sleeps=sleeps).evaluate("state", _QUESTIONS)
    assert response.answers["n0"].choice == "covered"
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("status", [429, 529])
async def test_rate_limit_gives_up_after_the_backoff_schedule(status):
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status)

    with pytest.raises(JevError) as excinfo:
        await _client(handler, sleeps=sleeps).evaluate("state", _QUESTIONS)
    assert excinfo.value.status_code == status
    assert sleeps == list(RETRY_BACKOFF_SECONDS)
    assert len(calls) == len(RETRY_BACKOFF_SECONDS) + 1


async def test_jitter_stretches_the_delay_by_at_most_a_quarter():
    responses = [httpx.Response(429), httpx.Response(200, json=_OK_BODY)]
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    client = JevClient(
        api_key="k",
        transport=httpx.MockTransport(lambda request: responses.pop(0)),
        sleep=_sleep,
        jitter=lambda: 1.0,
    )
    await client.evaluate("state", _QUESTIONS)
    assert sleeps == [pytest.approx(1.25)]


async def test_timeout_raises_without_retry():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(JevError, match="timed out"):
        await _client(handler).evaluate("state", _QUESTIONS)
    assert len(calls) == 1


async def test_transport_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=request)

    with pytest.raises(JevError, match="transport error"):
        await _client(handler).evaluate("state", _QUESTIONS)


async def test_missing_api_key_raises_before_any_request(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_OK_BODY)

    client = JevClient(transport=httpx.MockTransport(handler))
    with pytest.raises(JevError, match="JEV_API_KEY"):
        await client.evaluate("state", _QUESTIONS)
    assert calls == []


async def test_api_key_defaults_to_the_env_var(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "from-env")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=_OK_BODY)

    await JevClient(transport=httpx.MockTransport(handler)).evaluate("s", _QUESTIONS)
    assert seen == ["Bearer from-env"]


@pytest.mark.parametrize(
    "body, match",
    [
        ({"answers": []}, "no answers object"),
        ([], "no answers object"),
        ({"answers": {"n0": "covered"}}, "is not an object"),
        ({"answers": {"n0": {"confidence": 0.5, "probabilities": {}}}}, "has no choice"),
        (
            {"answers": {"n0": {"choice": "covered", "confidence": True, "probabilities": {}}}},
            "numeric confidence",
        ),
        (
            {"answers": {"n0": {"choice": "covered", "confidence": 0.5, "probabilities": 1}}},
            "has no probabilities",
        ),
    ],
)
async def test_malformed_body_raises(body, match):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(JevError, match=match):
        await _client(handler).evaluate("s", _QUESTIONS)


async def test_non_json_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(JevError, match="not JSON"):
        await _client(handler).evaluate("s", _QUESTIONS)


async def test_usage_tolerates_missing_or_odd_values():
    body = {**_OK_BODY, "usage": {"input_tokens": 5, "output_tokens": "x", "flag": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    response = await _client(handler).evaluate("s", _QUESTIONS)
    assert response.usage == {"input_tokens": 5}

    no_usage = {key: value for key, value in _OK_BODY.items() if key != "usage"}
    response = await _client(lambda request: httpx.Response(200, json=no_usage)).evaluate(
        "s", _QUESTIONS
    )
    assert response.usage == {}
