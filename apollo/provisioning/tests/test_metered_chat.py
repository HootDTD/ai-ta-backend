"""WU-3B2f — MeteredChat unit tests (fake usage; NO network, NO DB).

The metered client is the ONLY programmatic token signal in the §8B pipeline
(``apollo/agent/_llm.py`` reads ``response.usage`` only to LOG it, then discards
it). These tests inject a deterministic fake OpenAI client whose
``chat.completions.create`` returns a fake response carrying ``usage`` +
``choices[0].message.content``, and a plain ``_FakeIngestRun`` standing in for
the ``apollo_ingest_runs`` ORM row. They pin: content pass-through, cheap/main
model routing + explicit override, additive ``+=`` accumulation of
calls/tokens/cost onto the run row, the ``scrape_chat_fn`` positional adapter,
the cumulative-ceiling ``CostBudgetExceeded`` raise (counts accrued BEFORE the
raise), the unknown-model zero-cost path, and that ``_llm`` is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from apollo.provisioning import cost_constants
from apollo.provisioning.cost_constants import cost_usd_for
from apollo.provisioning.metered_chat import CostBudgetExceeded, MeteredChat


# --------------------------------------------------------------------------- #
# Deterministic fakes (no network, no DB).
# --------------------------------------------------------------------------- #
@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    usage: _FakeUsage
    choices: list[_FakeChoice]


class _FakeCompletions:
    def __init__(self, owner: _FakeClient) -> None:
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if not self._owner.queued:
            raise AssertionError("no fake response queued")
        return self._owner.queued.pop(0)


class _FakeChat:
    def __init__(self, owner: _FakeClient) -> None:
        self.completions = _FakeCompletions(owner)


class _FakeClient:
    """Stands in for ``openai.OpenAI()``. Records every ``create(**kwargs)`` and
    returns queued fake responses FIFO."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.queued = list(responses)
        self.calls: list[dict] = []
        self.chat = _FakeChat(self)


@dataclass
class _FakeIngestRun:
    """A plain stand-in for the apollo_ingest_runs ORM row (the five aggregates)."""

    id: int = 1
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))


def _resp(content: str, prompt: int, completion: int) -> _FakeResponse:
    return _FakeResponse(
        usage=_FakeUsage(prompt_tokens=prompt, completion_tokens=completion),
        choices=[_FakeChoice(message=_FakeMessage(content=content))],
    )


def _make(*, responses, ceiling=cost_constants.PER_DOCUMENT_TOKEN_CEILING, document_id=7):
    run = _FakeIngestRun()
    client = _FakeClient(responses)
    metered = MeteredChat(ingest_run=run, client=client, ceiling=ceiling, document_id=document_id)
    return metered, run, client


# --------------------------------------------------------------------------- #
# Content + routing.
# --------------------------------------------------------------------------- #
def test_cheap_returns_content():
    metered, _run, _client = _make(responses=[_resp("hello", 10, 5)])
    out = metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert out == "hello"


def test_cheap_routes_to_mini_model(monkeypatch):
    monkeypatch.delenv("APOLLO_CHEAP_MODEL", raising=False)
    metered, _run, client = _make(responses=[_resp("ok", 1, 1)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert client.calls[0]["model"] == "gpt-4o-mini"


def test_main_routes_to_main_model(monkeypatch):
    monkeypatch.delenv("MAIN_MODEL", raising=False)
    metered, _run, client = _make(responses=[_resp("ok", 1, 1)])
    metered.main(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert client.calls[0]["model"] == "gpt-4o"


def test_explicit_model_overrides_routing():
    metered, _run, client = _make(responses=[_resp("ok", 1, 1)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}], model="gpt-4o")
    assert client.calls[0]["model"] == "gpt-4o"


def test_cheap_passes_response_format_and_temperature():
    metered, _run, client = _make(responses=[_resp("{}", 1, 1)])
    metered.cheap(
        purpose="p",
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    kw = client.calls[0]
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["temperature"] == 0.3


def test_response_format_omitted_when_none():
    metered, _run, client = _make(responses=[_resp("x", 1, 1)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert "response_format" not in client.calls[0]


# --------------------------------------------------------------------------- #
# Accumulation.
# --------------------------------------------------------------------------- #
def test_single_call_accumulates_counts():
    metered, run, _client = _make(responses=[_resp("y", 120, 40)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert run.llm_calls == 1
    assert run.llm_tokens_in == 120
    assert run.llm_tokens_out == 40


def test_cost_accumulates_via_cost_usd_for():
    metered, run, _client = _make(responses=[_resp("y", 1_000_000, 1_000_000)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}], model="gpt-4o")
    assert run.llm_cost_usd == cost_usd_for("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)
    assert run.llm_cost_usd == Decimal("12.50")


def test_multiple_calls_accumulate_additively():
    # MUTATION: an `=` instead of `+=` makes this RED (second call would overwrite).
    metered, run, _client = _make(responses=[_resp("a", 100, 20), _resp("b", 50, 10)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert run.llm_calls == 2
    assert run.llm_tokens_in == 150
    assert run.llm_tokens_out == 30


def test_main_accumulates_too():
    metered, run, _client = _make(responses=[_resp("z", 200, 60)])
    metered.main(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert run.llm_calls == 1
    assert run.llm_tokens_in == 200
    assert run.llm_tokens_out == 60


# --------------------------------------------------------------------------- #
# scrape_chat_fn positional adapter (scrape.py:141 seam).
# --------------------------------------------------------------------------- #
def test_scrape_chat_fn_positional_adapter():
    metered, run, client = _make(responses=[_resp("parsed", 80, 30)])
    fn = metered.scrape_chat_fn("system instructions")
    out = fn("the chunk text")
    assert out == "parsed"
    assert run.llm_calls == 1
    # routed cheap
    assert client.calls[0]["model"] == "gpt-4o-mini"
    # the chunk text reached the user message; the system prompt is the system role.
    messages = client.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "system instructions"
    assert messages[-1]["role"] == "user"
    assert "the chunk text" in messages[-1]["content"]


# --------------------------------------------------------------------------- #
# Ceiling.
# --------------------------------------------------------------------------- #
def test_ceiling_not_breached_no_raise():
    metered, run, _client = _make(responses=[_resp("ok", 10, 10)], ceiling=1000)
    out = metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert out == "ok"
    assert run.llm_calls == 1


def test_ceiling_breached_raises_cost_budget_exceeded():
    # MUTATION: dropping the ceiling check makes this RED.
    metered, _run, _client = _make(responses=[_resp("ok", 60, 60)], ceiling=100)
    with pytest.raises(CostBudgetExceeded) as exc_info:
        metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    err = exc_info.value
    assert err.tokens == 120
    assert err.ceiling == 100
    assert err.document_id == 7


def test_counts_accrued_before_raise():
    # Even on the breaching call, the spend that triggered the abort is recorded.
    metered, run, _client = _make(responses=[_resp("ok", 60, 60)], ceiling=100)
    with pytest.raises(CostBudgetExceeded):
        metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert run.llm_calls == 1
    assert run.llm_tokens_in == 60
    assert run.llm_tokens_out == 60
    assert run.llm_cost_usd > Decimal("0")


def test_ceiling_breach_on_cumulative_across_calls():
    # First call stays under; the second pushes the CUMULATIVE over the ceiling.
    metered, run, _client = _make(responses=[_resp("a", 40, 40), _resp("b", 40, 40)], ceiling=150)
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(CostBudgetExceeded):
        metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert run.llm_calls == 2  # both calls counted


def test_ceiling_exact_boundary_does_not_raise():
    # Cumulative == ceiling is NOT over the line (strict > breach).
    metered, run, _client = _make(responses=[_resp("ok", 50, 50)], ceiling=100)
    out = metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert out == "ok"
    assert run.llm_calls == 1


# --------------------------------------------------------------------------- #
# Unknown model + _llm isolation.
# --------------------------------------------------------------------------- #
def test_unknown_model_accrues_counts_zero_cost():
    metered, run, _client = _make(responses=[_resp("ok", 100, 100)])
    metered.cheap(
        purpose="p",
        messages=[{"role": "user", "content": "x"}],
        model="some-future-model",
    )
    assert run.llm_calls == 1
    assert run.llm_tokens_in == 100
    assert run.llm_tokens_out == 100
    assert run.llm_cost_usd == Decimal("0")


def test_does_not_import_or_call_llm_module(monkeypatch):
    # MeteredChat re-invokes the client itself; it must NEVER call _llm.cheap_chat
    # / main_chat (which discard usage).
    import apollo.agent._llm as _llm

    def _boom(*a, **k):
        raise AssertionError("_llm must not be called by MeteredChat")

    monkeypatch.setattr(_llm, "cheap_chat", _boom)
    monkeypatch.setattr(_llm, "main_chat", _boom)

    metered, run, _client = _make(responses=[_resp("ok", 1, 1), _resp("ok", 1, 1)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    metered.main(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert run.llm_calls == 2


def test_cheap_uses_env_cheap_model_override(monkeypatch):
    monkeypatch.setenv("APOLLO_CHEAP_MODEL", "gpt-4o-mini-custom")
    metered, _run, client = _make(responses=[_resp("ok", 1, 1)])
    metered.cheap(purpose="p", messages=[{"role": "user", "content": "x"}])
    assert client.calls[0]["model"] == "gpt-4o-mini-custom"
