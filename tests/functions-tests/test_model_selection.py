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


def test_keyword_extraction_default_model(monkeypatch):
    monkeypatch.delenv("KEYWORD_MODEL", raising=False)
    monkeypatch.delenv("PARSER_MODEL", raising=False)
    captured: dict = {}
    # Measured A/B: gpt-4o is ~2x faster than gpt-4o-mini for this call.
    payload = json.dumps({"context_summary": "x", "ranked_terms": []})
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    mai.extract_and_filter_keywords("What is Bernoulli's equation?", subject="Physics")
    assert captured["model"] == "gpt-4o"


def test_keyword_extraction_env_override(monkeypatch):
    # Sentinel value, deliberately NOT the old default (gpt-4o) so this test
    # cannot pass coincidentally before KEYWORD_MODEL is wired up.
    monkeypatch.setenv("KEYWORD_MODEL", "sentinel-keyword-model")
    captured: dict = {}
    payload = json.dumps({"context_summary": "x", "ranked_terms": []})
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    mai.extract_and_filter_keywords("What is lift?", subject="Physics")
    assert captured["model"] == "sentinel-keyword-model"


def test_snippet_scorer_default_model(monkeypatch):
    # Measured A/B (4 runs): gpt-4o median 2.54s/call vs gpt-4o-mini 3.30s —
    # mini is slower, so the default stays gpt-4o. PARSER_MODEL must no
    # longer leak into scorer model resolution.
    monkeypatch.delenv("CITATION_SCORER_MODEL", raising=False)
    monkeypatch.setenv("PARSER_MODEL", "sentinel-parser-model")
    captured: dict = {}
    payload = json.dumps(
        {
            "relevance": 0.9,
            "directness": 0.8,
            "score": 0.85,
            "context": "c",
            "answer": "a",
        }
    )
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, payload))
    snippet = types.SimpleNamespace(
        citation_marker="[S1]",
        page=3,
        why="",
        text="Bernoulli",
        section_path="",
        id="s1",
    )
    result = mai._score_and_answer_snippet("q?", snippet, 1.0, "bernoulli")
    assert captured["model"] == "gpt-4o"
    assert result["marker"] == "[S1]"


def test_snippet_scorer_env_override(monkeypatch):
    monkeypatch.setenv("CITATION_SCORER_MODEL", "sentinel-scorer-model")
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_chat_client(captured, "{}"))
    snippet = types.SimpleNamespace(
        citation_marker="[S1]",
        page=None,
        why="",
        text="t",
        section_path="",
        id="s1",
    )
    mai._score_and_answer_snippet("q?", snippet, 1.0, "term")
    assert captured["model"] == "sentinel-scorer-model"
