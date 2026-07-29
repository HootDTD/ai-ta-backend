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
