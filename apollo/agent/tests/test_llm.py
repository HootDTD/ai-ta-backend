"""apollo.agent._llm.main_chat model resolution.

After the flag reset the main tier is pinned to ``config.models.MAIN_MODEL``;
an explicit ``model=`` argument still overrides it.
"""

from __future__ import annotations

import types

import apollo.agent._llm as llm


def _fake_client(captured: dict, content: str = "ok"):
    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = types.SimpleNamespace(content=content)
            usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=2)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice], usage=usage)

    chat = types.SimpleNamespace(completions=_Completions())
    return types.SimpleNamespace(chat=chat)


def test_main_chat_uses_pinned_main_model(monkeypatch):
    monkeypatch.setattr("config.models.MAIN_MODEL", "main-sentinel")
    captured: dict = {}
    monkeypatch.setattr(llm, "_client", lambda: _fake_client(captured))

    out = llm.main_chat(purpose="p", messages=[{"role": "user", "content": "x"}])

    assert out == "ok"
    assert captured["model"] == "main-sentinel"


def test_bounded_client_defaults(monkeypatch):
    captured: dict = {}

    class _CaptureOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.delenv("APOLLO_OPENAI_TIMEOUT_S", raising=False)
    monkeypatch.delenv("APOLLO_OPENAI_MAX_RETRIES", raising=False)
    monkeypatch.setattr(llm, "OpenAI", _CaptureOpenAI)

    client = llm.bounded_client()

    assert isinstance(client, _CaptureOpenAI)
    assert captured == {"timeout": 90.0, "max_retries": 1}


def test_bounded_client_env_overrides(monkeypatch):
    captured: dict = {}

    class _CaptureOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("APOLLO_OPENAI_TIMEOUT_S", "30")
    monkeypatch.setenv("APOLLO_OPENAI_MAX_RETRIES", "0")
    monkeypatch.setattr(llm, "OpenAI", _CaptureOpenAI)

    llm.bounded_client()

    assert captured == {"timeout": 30.0, "max_retries": 0}


def test_client_helper_is_bounded(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(llm, "bounded_client", lambda: sentinel)

    assert llm._client() is sentinel


def test_main_chat_explicit_model_overrides_constant(monkeypatch):
    monkeypatch.setattr("config.models.MAIN_MODEL", "main-sentinel")
    captured: dict = {}
    monkeypatch.setattr(llm, "_client", lambda: _fake_client(captured))

    llm.main_chat(
        purpose="p",
        messages=[{"role": "user", "content": "x"}],
        model="explicit",
        response_format={"type": "json_object"},
    )

    assert captured["model"] == "explicit"
    assert captured["response_format"] == {"type": "json_object"}
