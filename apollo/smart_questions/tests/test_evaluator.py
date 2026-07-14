import json

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.smart_questions import evaluator
from apollo.smart_questions.planner import NodeCoverage


def _graph(*node_ids: str) -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id=node_id,
                attempt_id=1,
                source="reference",
                content={"concept": node_id, "meaning": node_id.upper()},
            )
            for node_id in node_ids
        ],
        edges=[],
    )


@pytest.mark.asyncio
async def test_maps_continuous_credit_to_question_states(monkeypatch):
    def fake_call(**kwargs):
        return json.dumps(
            {
                "nodes": [
                    {"node_id": "a", "state": "covered", "credit": 0.7, "ask_hint": ""},
                    {
                        "node_id": "b",
                        "state": "partial",
                        "credit": 0.3,
                        "ask_hint": "ask why it occurs",
                    },
                    {
                        "node_id": "c",
                        "state": "missing",
                        "credit": 0.0,
                        "ask_hint": "ask for an example",
                    },
                ]
            }
        )

    monkeypatch.setattr(evaluator, "_call_evaluator", fake_call)
    result = await evaluator.evaluate_reference_coverage(
        transcript=[("student", "hello")],
        reference_graph=_graph("a", "b", "c"),
        problem=type("P", (), {"problem_text": "p"})(),
    )
    assert [(item.node_id, item.state, item.ask_hint) for item in result] == [
        ("a", "covered", ""),
        ("b", "partial", "ask why it occurs"),
        ("c", "missing", "ask for an example"),
    ]


@pytest.mark.asyncio
async def test_missing_or_hallucinated_verdicts_fail_closed(monkeypatch):
    monkeypatch.setattr(
        evaluator,
        "_call_evaluator",
        lambda **kwargs: (
            '{"nodes":[{"node_id":"invented","state":"covered","credit":1.0,"ask_hint":""}]}'
        ),
    )
    result = await evaluator.evaluate_reference_coverage(
        transcript=[], reference_graph=_graph("a"), problem=type("P", (), {"problem_text": "p"})()
    )
    assert result == [NodeCoverage("a", "missing", 0.0)]


@pytest.mark.asyncio
async def test_malformed_ask_hint_degrades_to_empty(monkeypatch):
    def fake_call(**kwargs):
        return json.dumps(
            {
                "nodes": [
                    {"node_id": "a", "state": "missing", "credit": 0.0, "ask_hint": 42},
                    {
                        "node_id": "b",
                        "state": "missing",
                        "credit": 0.0,
                        "ask_hint": "x" * 500,
                    },
                    {"node_id": "c", "state": "missing", "credit": 0.0},
                ]
            }
        )

    monkeypatch.setattr(evaluator, "_call_evaluator", fake_call)
    result = await evaluator.evaluate_reference_coverage(
        transcript=[],
        reference_graph=_graph("a", "b", "c"),
        problem=type("P", (), {"problem_text": "p"})(),
    )
    assert [item.ask_hint for item in result] == ["", "", ""]


@pytest.mark.asyncio
async def test_evaluator_prompt_requests_public_surface_hints(monkeypatch):
    captured: dict = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return '{"nodes":[]}'

    monkeypatch.setattr(evaluator, "_call_evaluator", fake_call)
    await evaluator.evaluate_reference_coverage(
        transcript=[("student", "hi")],
        reference_graph=_graph("a"),
        problem=type("P", (), {"problem_text": "p"})(),
    )
    # The hint contract lives in the system prompt; _call_evaluator owns it, so
    # here we only pin the plumbing surface: items + student messages flow in.
    assert captured["items"][0]["node_id"] == "a"
    assert captured["student_messages"] == ["hi"]


def test_schema_requires_ask_hint():
    schema = evaluator._schema()
    item = schema["schema"]["properties"]["nodes"]["items"]
    assert "ask_hint" in item["required"]
    assert item["properties"]["ask_hint"]["type"] == "string"


def test_system_prompt_forbids_revealing_node_content():
    prompt = evaluator._hint_instruction()
    lowered = prompt.casefold()
    assert "problem text" in lowered
    assert "student" in lowered
    assert "never" in lowered


def _patch_rewrite_client(monkeypatch, captured: dict, reply: str):
    from types import SimpleNamespace

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(evaluator, "OpenAI", lambda: fake_client)


def test_rewrite_hint_returns_trimmed_rewrite_and_bans_words(monkeypatch):
    captured: dict = {}
    _patch_rewrite_client(monkeypatch, captured, reply="  ask them to go deeper on x  ")
    result = evaluator.rewrite_hint(
        hint="ask about the private part",
        forbidden_words=["private"],
        problem_text="Explain x.",
        student_messages=["x matters"],
    )
    assert result == "ask them to go deeper on x"
    payload = json.loads(captured["messages"][1]["content"])
    assert payload["forbidden_words"] == ["private"]
    assert payload["problem"] == "Explain x."
    assert payload["student_messages"] == ["x matters"]


def test_rewrite_hint_discards_overlong_or_empty_output(monkeypatch):
    _patch_rewrite_client(monkeypatch, {}, reply="x" * 301)
    assert (
        evaluator.rewrite_hint(hint="h", forbidden_words=[], problem_text="p", student_messages=[])
        == ""
    )
    _patch_rewrite_client(monkeypatch, {}, reply="")
    assert (
        evaluator.rewrite_hint(hint="h", forbidden_words=[], problem_text="p", student_messages=[])
        == ""
    )
