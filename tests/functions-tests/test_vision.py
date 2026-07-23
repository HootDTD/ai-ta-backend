"""vision_direct_answer model selection.

After the 2026-07 flag reset, the main tier is pinned to ``config.models.MAIN_MODEL``;
``VISION_ANSWER_MODEL`` still overrides it per-surface from the environment.
"""

from __future__ import annotations

import types

import ai.vision as vision


def _fake_openai_cls(captured: dict, content: str = "answer"):
    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = types.SimpleNamespace(content=content)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice])

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=_Completions())

    return _OpenAI


def test_vision_direct_answer_defaults_to_pinned_main_model(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VISION_ANSWER_MODEL", raising=False)
    monkeypatch.setattr("config.models.MAIN_MODEL", "vision-main-sentinel")
    captured: dict = {}
    monkeypatch.setattr("openai.OpenAI", _fake_openai_cls(captured))

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")
    out = vision.vision_direct_answer([str(img)], question_hint="what is this?")

    assert out == "answer"
    assert captured["model"] == "vision-main-sentinel"


def test_vision_direct_answer_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VISION_ANSWER_MODEL", "vision-override")
    captured: dict = {}
    monkeypatch.setattr("openai.OpenAI", _fake_openai_cls(captured))

    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")
    out = vision.vision_direct_answer([str(img)])

    assert out == "answer"
    assert captured["model"] == "vision-override"
