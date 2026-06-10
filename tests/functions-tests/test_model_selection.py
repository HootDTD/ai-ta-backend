"""Env-driven model selection for auxiliary LLM calls (keywords, snippet scoring)."""
from __future__ import annotations

import json
import types

import ai.main_ai as mai


def _fake_chat_client(captured: dict, content: str = "{}"):
    class _Completions:
        def create(self, *a, **k):
            captured.update(k)
            msg = types.SimpleNamespace(content=content)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice])
    chat = types.SimpleNamespace(completions=_Completions())
    return types.SimpleNamespace(chat=chat)


def test_keyword_extraction_defaults_to_mini_model(monkeypatch):
    monkeypatch.delenv("KEYWORD_MODEL", raising=False)
    monkeypatch.delenv("PARSER_MODEL", raising=False)
    captured: dict = {}
    payload = json.dumps({"context_summary": "x", "ranked_terms": []})
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    mai.extract_and_filter_keywords("What is Bernoulli's equation?", subject="Physics")
    assert captured["model"] == "gpt-4o-mini"


def test_keyword_extraction_env_override(monkeypatch):
    # Sentinel value, deliberately NOT the old default (gpt-4o) so this test
    # cannot pass coincidentally before KEYWORD_MODEL is wired up.
    monkeypatch.setenv("KEYWORD_MODEL", "sentinel-keyword-model")
    captured: dict = {}
    payload = json.dumps({"context_summary": "x", "ranked_terms": []})
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    mai.extract_and_filter_keywords("What is lift?", subject="Physics")
    assert captured["model"] == "sentinel-keyword-model"
