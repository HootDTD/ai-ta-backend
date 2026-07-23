"""solve_with_bundle pins the model + reasoning effort to config.models constants.

The flag reset replaced ``os.getenv("MAIN_MODEL"/"MAIN_REASONING_EFFORT", …)`` in
``_prepare_solve_prompt`` / ``solve_with_bundle`` with the pinned constants. These
tests drive the blocking solve path (mocked client) and assert what reaches the
OpenAI call.
"""

from __future__ import annotations

import json
import types

import ai.main_ai as mai
from config.contracts import ParsedTask, ResearchBundle, ResearchMetadata


def _fake_client(captured: dict, content: str):
    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = types.SimpleNamespace(content=content)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice])

    chat = types.SimpleNamespace(completions=_Completions())
    return types.SimpleNamespace(chat=chat)


def _empty_bundle() -> ResearchBundle:
    # Empty snippets skip the scoring pool entirely — the prompt still builds and
    # _prepare_solve_prompt returns the pinned model.
    return ResearchBundle(metadata=ResearchMetadata(question="What is lift?"), snippets=[])


def test_solve_with_bundle_pins_model_and_reasoning_effort(monkeypatch):
    monkeypatch.setattr("config.models.MAIN_MODEL", "gpt-5.1")
    monkeypatch.setattr("config.models.MAIN_REASONING_EFFORT", "low")
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_client(captured, json.dumps({"steps": "ok"})))

    solution = mai.solve_with_bundle(
        ParsedTask(problem_type="conceptual"), _empty_bundle(), subject="Physics"
    )

    assert captured["model"] == "gpt-5.1"
    assert captured["reasoning_effort"] == "low"  # gpt-5.1 is a reasoning model
    assert "temperature" not in captured
    assert solution.steps == "ok"


def test_solve_with_bundle_non_reasoning_model_omits_effort(monkeypatch):
    monkeypatch.setattr("config.models.MAIN_MODEL", "gpt-4o")
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_client(captured, json.dumps({"steps": "ok"})))

    mai.solve_with_bundle(ParsedTask(problem_type="conceptual"), _empty_bundle(), subject="Physics")

    assert captured["model"] == "gpt-4o"
    assert "reasoning_effort" not in captured  # gpt-4o rejects reasoning_effort
    assert captured["temperature"] == 0
