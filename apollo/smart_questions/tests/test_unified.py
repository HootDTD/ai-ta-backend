from types import SimpleNamespace

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.smart_questions import unified


def _graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": "pressure", "meaning": "force divided by area"},
            ),
            build_node(
                node_type="procedure_step",
                node_id="b",
                attempt_id=1,
                source="reference",
                content={"action": "multiply by area", "purpose": "find force"},
            ),
        ],
        edges=[],
    )


def _payload(*, action="ask", target="a", reply="Why does your pressure step work?"):
    return {
        "nodes": [
            {"node_id": "a", "state": "partial", "credit": 0.4},
            {"node_id": "b", "state": "missing", "credit": 0.0},
        ],
        "action": action,
        "target_node_id": target,
        "reply": reply,
    }


@pytest.mark.asyncio
async def test_one_call_returns_coverage_and_confused_reply(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs["payload"])
        return __import__("json").dumps(_payload())

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    result = await unified.evaluate_and_ask(
        transcript=[("student", "I use pressure here")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )

    assert len(calls) == 1
    assert result.action == "ask"
    assert result.target_node_id == "a"
    assert result.reply == "Why does your pressure step work?"
    assert [(item.node_id, item.state) for item in result.coverage] == [
        ("a", "partial"),
        ("b", "missing"),
    ]


@pytest.mark.asyncio
async def test_direct_private_answer_leak_uses_content_free_fallback(monkeypatch):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: __import__("json").dumps(
            _payload(reply="Is pressure force divided by area?")
        ),
    )
    result = await unified.evaluate_and_ask(
        transcript=[("student", "I use pressure")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )
    assert result.reply == unified._SAFE_FALLBACK
    assert "force divided by area" not in result.reply


def test_private_token_leak_is_rejected_even_without_full_phrase():
    assert (
        unified._safe_reply(
            "Could you explain force?",
            reference_graph=_graph(),
            student_messages=["I use pressure"],
        )
        == unified._SAFE_FALLBACK
    )


@pytest.mark.asyncio
async def test_invalid_target_and_malformed_coverage_fail_safe(monkeypatch):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: (
            '{"nodes":[{"node_id":"invented","state":"covered","credit":1}],'
            '"action":"ask","target_node_id":"invented","reply":"What is the answer?"}'
        ),
    )
    result = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )
    assert result.target_node_id == "a"
    assert result.reply == unified._SAFE_FALLBACK
    assert all(item.state == "missing" for item in result.coverage)


@pytest.mark.asyncio
async def test_malformed_json_and_bad_items_default_to_missing(monkeypatch):
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: "not json")
    malformed = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )
    assert all(item.state == "missing" for item in malformed.coverage)

    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: "[]")
    wrong_shape = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )
    assert wrong_shape.reply == unified._SAFE_FALLBACK

    payload = _payload()
    payload["nodes"] = [None, {"node_id": "a", "state": "partial", "credit": "bad"}]
    payload["target_node_id"] = ["a"]
    payload["reply"] = 42
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: __import__("json").dumps(payload),
    )
    bad_items = await unified.evaluate_and_ask(
        transcript=[("student", "pressure")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )
    assert bad_items.coverage[0] == unified.NodeCoverage("a", "partial", 0.0)


@pytest.mark.asyncio
async def test_done_only_when_no_unasked_gap_remains(monkeypatch):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: __import__("json").dumps(_payload(action="done", target=None, reply=None)),
    )
    premature = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids=set(),
    )
    assert premature.action == "ask"
    assert premature.reply == unified._SAFE_FALLBACK

    exhausted = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        already_asked_node_ids={"a", "b"},
    )
    assert exhausted.action == "done"
    assert exhausted.reply is None


def test_call_uses_gpt_5_2_by_default_and_strict_output(monkeypatch):
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    monkeypatch.delenv("APOLLO_UNIFIED_QUESTION_MODEL", raising=False)
    monkeypatch.setattr(
        unified,
        "OpenAI",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )
    unified._call_unified(payload={})
    assert captured["model"] == "gpt-5.2"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert "Never state, name, paraphrase" in captured["messages"][0]["content"]
    assert "temperature" not in captured
