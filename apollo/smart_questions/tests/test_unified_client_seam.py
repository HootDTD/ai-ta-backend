"""S3 — the injectable client seam on the per-turn questioning call.

P3.2's validation and P3.1 Phase 0 both need to replay a recorded transcript
through `evaluate_and_ask` with **no network**, and the P3-campaign trick of
patching the at-Done adjudicator does not reach this producer (it is the
per-TURN LLM). The seam is deliberately minimal: the injected object must
satisfy exactly ``client.chat.completions.create(**kwargs)`` returning an
object with ``choices[0].message.content: str``. Nothing else about it is
assumed, and ``client=None`` is byte-identical to the pre-seam path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.smart_questions import unified

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _run_mocked_calls_inline(monkeypatch):
    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(unified.asyncio, "to_thread", inline)


class _RecordedClient:
    """The whole seam contract, and nothing more."""

    def __init__(self, *responses: dict):
        self.calls: list[dict] = []
        self._responses = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._responses.pop(0) if self._responses else {}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )


def _graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id="b",
                attempt_id=1,
                source="reference",
                content={"action": "multiply by area", "purpose": "find force"},
            )
        ],
        edges=[],
    )


def _kwargs(**overrides):
    values = {
        "transcript": [("student", "I use pressure here")],
        "reference_graph": _graph(),
        "problem": SimpleNamespace(problem_text="Why does pressure work?"),
        "tally_state": (unified.TallyState("b", "multiply by area", "missing"),),
        "budget": unified.QuestionBudget(questions_asked=0, cap=8),
    }
    values.update(overrides)
    return values


def _draft(**overrides):
    draft = {
        "tally_updates": [],
        "action": "ask",
        "target_node_id": "b",
        "acknowledgement": "That helps.",
        "question": "What do you do with the area?",
    }
    draft.update(overrides)
    return draft


def _explode():
    raise AssertionError("bounded_client() must not be resolved when a client is injected")


@pytest.mark.asyncio
async def test_injected_client_is_used_and_bounded_client_never_called(monkeypatch):
    monkeypatch.setattr(unified, "bounded_client", _explode)
    client = _RecordedClient(_draft())
    result = await unified.evaluate_and_ask(**_kwargs(), client=client)
    assert result.action == "ask"
    assert result.target_node_id == "b"
    assert len(client.calls) == 1


def test_default_path_uses_bounded_client_unchanged(monkeypatch):
    """`client=None` resolves `bounded_client()` exactly as before the seam."""
    client = _RecordedClient(_draft())
    monkeypatch.setattr(unified, "bounded_client", lambda: client)
    raw = unified._call_unified(payload={"public_problem": "x"})
    assert json.loads(raw)["action"] == "ask"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_regenerate_uses_the_same_injected_client(monkeypatch):
    """The one regenerate slot must not fall back to the real client — a replay
    that silently hit the network on retries would report live numbers as
    recorded ones."""
    monkeypatch.setattr(unified, "bounded_client", _explode)
    malformed = _draft(question="Tell me more.")  # no '?' -> the repair turn fires
    client = _RecordedClient(malformed, _draft())
    result = await unified.evaluate_and_ask(**_kwargs(), client=client)
    assert len(client.calls) == 2
    assert result.question == "What do you do with the area?"


@pytest.mark.asyncio
async def test_injected_client_sees_the_level_0_schema_by_default(monkeypatch):
    monkeypatch.setattr(unified, "bounded_client", _explode)
    client = _RecordedClient(_draft())
    await unified.evaluate_and_ask(**_kwargs(), client=client)
    item = client.calls[0]["response_format"]["json_schema"]["schema"]["properties"][
        "tally_updates"
    ]["items"]
    assert item["required"] == ["node_id", "status", "evidence"]


@pytest.mark.asyncio
async def test_wrongness_flag_reaches_both_the_schema_and_the_system_turn(monkeypatch):
    """The producer is off unless the caller says otherwise — this is the only
    switch, and it moves the schema and the prompt together."""
    monkeypatch.setattr(unified, "bounded_client", _explode)
    client = _RecordedClient(_draft(), _draft())
    await unified.evaluate_and_ask(**_kwargs(), client=client, wrongness=True)
    call = client.calls[0]
    item = call["response_format"]["json_schema"]["schema"]["properties"]["tally_updates"]["items"]
    assert item["required"] == ["node_id", "status", "evidence", "wrongness", "contradiction"]
    assert "WRONGNESS DUTY:" in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_regenerate_keeps_the_wrongness_schema(monkeypatch):
    """A repair turn must not silently drop back to the level-0 schema, or the
    model would be told to answer in a shape its system turn does not describe."""
    monkeypatch.setattr(unified, "bounded_client", _explode)
    client = _RecordedClient(_draft(question="Tell me more."), _draft())
    await unified.evaluate_and_ask(**_kwargs(), client=client, wrongness=True)
    for call in client.calls:
        item = call["response_format"]["json_schema"]["schema"]["properties"]["tally_updates"][
            "items"
        ]
        assert "wrongness" in item["required"]


@pytest.mark.asyncio
async def test_budget_exhausted_discard_is_logged_and_still_returns_no_updates(caplog):
    """R2, PRESERVED: the `budget_exhausted` branch returns before the call, so
    the final turn's tally updates — wrongness included — are lost. That
    behaviour is deliberately UNCHANGED; it is only made observable so the
    campaign can size the undercount."""
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            **_kwargs(budget=unified.QuestionBudget(questions_asked=8, cap=8))
        )
    assert result.tally_updates == ()
    assert result.action == "done"
    assert "apollo_tally_updates_discarded attempt_budget=8/8" in caplog.text
