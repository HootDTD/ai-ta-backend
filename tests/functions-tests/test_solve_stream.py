"""solve_with_bundle_stream yields reasoning + token deltas, then a ProposedSolution."""
from __future__ import annotations

import types

import ai.main_ai as mai


def _evt(type_, **kw):
    return types.SimpleNamespace(type=type_, **kw)


def _fake_responses_stream(events, captured=None):
    class _Responses:
        def create(self, *a, **k):
            assert k.get("stream") is True
            if captured is not None:
                captured.update(k)
            return iter(events)
    return types.SimpleNamespace(responses=_Responses())


def test_stream_yields_reasoning_then_tokens_then_solution(monkeypatch):
    json_pieces = ['{"steps": "Energy is ', 'conserved.", ',
                   '"equations_used": [], "assumptions": [], "not_relevant": false}']
    events = [
        _evt("response.reasoning_summary_text.delta", delta="Identifying the concept. "),
        _evt("response.reasoning_summary_text.delta", delta="Checking S1. "),
        *[_evt("response.output_text.delta", delta=p) for p in json_pieces],
        _evt("response.completed"),
    ]
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(events, captured))
    monkeypatch.setattr(mai, "_prepare_solve_prompt", lambda *a, **k: ("SYS", "USER", "gpt-5"))

    reasoning, tokens, solution = [], [], None
    for kind, payload in mai.solve_with_bundle_stream(object(), object(), subject="Physics"):
        if kind == "reasoning":
            reasoning.append(payload)
        elif kind == "token":
            tokens.append(payload)
        elif kind == "solution":
            solution = payload

    assert "".join(reasoning) == "Identifying the concept. Checking S1. "
    assert "".join(tokens) == "Energy is conserved."
    assert solution is not None
    assert solution.steps == "Energy is conserved."
    assert "reasoning" in captured


def test_non_reasoning_model_omits_reasoning_param(monkeypatch):
    captured: dict = {}
    events = [
        _evt("response.output_text.delta", delta='{"steps": "Hi.", "not_relevant": false}'),
        _evt("response.completed"),
    ]
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(events, captured))
    monkeypatch.setattr(mai, "_prepare_solve_prompt", lambda *a, **k: ("SYS", "USER", "gpt-4o"))
    list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    assert "reasoning" not in captured
    assert captured.get("temperature") == 0


def test_empty_stream_yields_empty_solution(monkeypatch):
    events = [_evt("response.completed")]
    monkeypatch.setattr(mai, "_client", lambda: _fake_responses_stream(events))
    monkeypatch.setattr(mai, "_prepare_solve_prompt", lambda *a, **k: ("SYS", "USER", "gpt-5"))
    out = list(mai.solve_with_bundle_stream(object(), object(), subject="X"))
    kinds = [k for k, _ in out]
    assert kinds[-1] == "solution"
    assert out[-1][1].steps == ""
