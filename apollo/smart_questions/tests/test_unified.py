import json
from types import SimpleNamespace

import pytest

from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.smart_questions import unified


@pytest.fixture(autouse=True)
def _run_mocked_calls_inline(monkeypatch):
    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(unified.asyncio, "to_thread", inline)


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


def _state(*, asked=0, status="missing"):
    return (
        unified.TallyState("a", "pressure", status, times_asked=asked),
        unified.TallyState("b", "multiply by area", "missing"),
    )


def _draft(
    *,
    action="ask",
    target="a",
    acknowledgement="That helps.",
    question="Why does pressure work?",
    updates=None,
):
    return {
        "tally_updates": updates
        if updates is not None
        else [
            {
                "node_id": "a",
                "status": "tentative",
                "evidence": {"turn_id": 0, "quote": "I use pressure"},
                "student_declined": False,
            }
        ],
        "action": action,
        "target_node_id": target,
        "acknowledgement": acknowledgement,
        "question": question,
    }


def _kwargs(**overrides):
    values = {
        "transcript": [("student", "I use pressure here")],
        "reference_graph": _graph(),
        "problem": SimpleNamespace(problem_text="Why does pressure work?"),
        "tally_state": _state(),
        "budget": unified.QuestionBudget(questions_asked=0, cap=8),
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_one_call_round_trips_tally_payload_budget_and_update(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(_draft())

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    result = await unified.evaluate_and_ask(**_kwargs())

    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert list(payload) == [
        "public_problem",
        "public_question_parts",
        "private_reference_nodes",
        "private_reference_edges",
        "tally_state",
        "budget",
        "transcript",
    ]
    assert payload["tally_state"][0] == {
        "node_id": "a",
        "label": "pressure",
        "status": "missing",
        "evidence": [],
        "student_declined": False,
        "times_asked": 0,
        "last_asked_turn": None,
    }
    assert payload["budget"] == {"questions_asked": 0, "cap": 8}
    assert payload["transcript"][0]["turn_id"] == 0
    assert result.tally_updates == (
        unified.TallyUpdate("a", "tentative", unified.EvidenceQuote(0, "I use pressure"), False),
    )
    assert result.reply == "That helps. Why does pressure work?"


@pytest.mark.asyncio
async def test_done_is_honored_with_missing_nodes(monkeypatch, caplog):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: json.dumps(_draft(action="done", target=None, question=None)),
    )
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(**_kwargs())
    assert result.action == "done"
    assert result.target_node_id is None
    assert "action=done" in caplog.text


@pytest.mark.asyncio
async def test_budget_exhausted_skips_llm_and_below_cap_calls(monkeypatch, caplog):
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_draft())

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    with caplog.at_level("INFO"):
        exhausted = await unified.evaluate_and_ask(**_kwargs(budget=unified.QuestionBudget(8, 8)))
    assert exhausted.action == "done"
    assert calls == 0
    assert "fallback_reason=budget_exhausted" in caplog.text
    assert (
        await unified.evaluate_and_ask(**_kwargs(budget=unified.QuestionBudget(7, 8)))
    ).action == "ask"
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_target_defaults_for_logging_without_replacing_question(monkeypatch):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: json.dumps(_draft(target="unknown", question="What happens next?")),
    )
    result = await unified.evaluate_and_ask(**_kwargs())
    assert result.target_node_id == "a"
    assert result.question == "What happens next?"


@pytest.mark.parametrize(
    ("reply", "flagged"),
    [
        ("Did it begin in 1970?", True),
        ("What did Toffler say?", True),
        ("How does change over time happen?", True),
        ("Does this change anything about how people live day to day?", False),
    ],
)
def test_belt_three_atom_classes_and_ordinary_english(reply, flagged):
    meaning = "change over time" if flagged else "decision paralysis from excess options"
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="future",
                attempt_id=1,
                source="reference",
                content={"concept": "Toffler", "meaning": meaning},
            )
        ],
        edges=[],
    )
    assert (
        unified._leaks_private_content(
            reply,
            reference_graph=graph,
            public_text="Why does it occur?",
            student_messages=[],
        )
        is flagged
    )


def test_student_said_private_atoms_and_faithful_ack_pass():
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="private",
                attempt_id=1,
                source="reference",
                content={"concept": "Toffler", "meaning": "change over time in 1970"},
            )
        ],
        edges=[],
    )
    student = "I think Toffler described change over time in 1970"
    verdict = unified._belt_verdict(
        "So you think Toffler described change over time in 1970. What happens next?",
        reference_graph=graph,
        public_text="What happens?",
        student_messages=[student],
    )
    assert not verdict.hit
    assert not verdict.malformed


def test_private_phrase_of_only_function_words_is_flagged():
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="phrase",
                attempt_id=1,
                source="reference",
                content={"concept": "private", "meaning": "after all"},
            )
        ],
        edges=[],
    )
    verdict = unified._belt_verdict(
        "What happens after all?",
        reference_graph=graph,
        public_text="What happens?",
        student_messages=[],
    )
    assert verdict.private_vocabulary == ()
    assert "after all" in verdict.private_phrases


@pytest.mark.asyncio
async def test_belt_hit_is_served_as_is_and_logged(monkeypatch, caplog):
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_draft(acknowledgement=None, question="What did Toffler say?"))

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": "Toffler", "meaning": "private theory"},
            )
        ],
        edges=[],
    )
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            **_kwargs(
                reference_graph=graph,
                tally_state=(unified.TallyState("a", "Toffler", "missing"),),
            )
        )
    assert calls == 1
    assert result.reply == "What did Toffler say?"
    assert "fallback_reason=None" in caplog.text
    assert "belt_hit_served=True" in caplog.text


@pytest.mark.asyncio
async def test_malformed_gets_one_regenerate_with_append_only_prefix(monkeypatch, caplog):
    drafts = [
        _draft(question="This is not a question"),
        _draft(acknowledgement=None, question="What happens next?"),
    ]
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(drafts[len(calls) - 1])

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(**_kwargs())
    assert len(calls) == 2
    assert calls[1]["messages"][:2] == calls[0]["messages"]
    assert calls[1]["messages"][2] == {"role": "assistant", "content": json.dumps(drafts[0])}
    assert calls[1]["messages"][3]["content"] == unified._MALFORMED_FEEDBACK
    assert result.reply == "What happens next?"
    assert "fallback_reason=malformed_regenerated" in caplog.text
    assert "belt_hit_served=False" in caplog.text


@pytest.mark.asyncio
async def test_malformed_regenerate_with_belt_hit_is_served(monkeypatch, caplog):
    drafts = [
        _draft(question="This is not a question"),
        _draft(acknowledgement=None, question="What did Toffler say?"),
    ]
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        result = drafts[calls]
        calls += 1
        return json.dumps(result)

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": "Toffler", "meaning": "private theory"},
            )
        ],
        edges=[],
    )
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            **_kwargs(
                reference_graph=graph,
                tally_state=(unified.TallyState("a", "Toffler", "missing"),),
            )
        )
    assert calls == 2
    assert result.reply == "What did Toffler say?"
    assert "fallback_reason=malformed_regenerated" in caplog.text
    assert "belt_hit_served=True" in caplog.text


@pytest.mark.asyncio
async def test_double_malformed_serves_verbatim_public_clause(monkeypatch, caplog):
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_draft(question="This is not a question"))

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(**_kwargs())
    assert calls == 2
    assert result.reply == "Why does pressure work?"
    assert "fallback_reason=malformed_exhausted" in caplog.text
    assert "belt_hit_served=False" in caplog.text


@pytest.mark.asyncio
async def test_repeated_question_is_served_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: json.dumps(_draft(acknowledgement=None)),
    )
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            **_kwargs(transcript=[("apollo", "Why does pressure work?")])
        )
    assert result.question == "Why does pressure work?"
    assert "repeated_question_served=True" in caplog.text


def test_prompt_hygiene_schema_and_model_call(monkeypatch):
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    monkeypatch.delenv("APOLLO_UNIFIED_QUESTION_MODEL", raising=False)
    monkeypatch.delenv("APOLLO_UNIFIED_QUESTION_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(
        unified,
        "OpenAI",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )
    unified._call_unified(payload={})
    prompt = captured["messages"][0]["content"]
    assert captured["model"] == "gpt-5.2"
    assert captured["reasoning_effort"] == "medium"
    assert "tally_updates" in captured["response_format"]["json_schema"]["schema"]["required"]
    assert "future shock" not in prompt.casefold()
    assert "every student-facing subject-matter word" not in prompt
    assert "Recompute the entire tally" not in prompt
    assert "Never introduce an example, relationship" not in prompt


def test_prompt_encodes_confirm_once_reprobe_policy():
    prompt = unified._SYSTEM_PROMPT
    # Re-probing must lean on the durable counter, vary the wording, and cap at two asks per node.
    assert "times_asked" in prompt
    assert "different angle" in prompt
    assert "at most twice" in prompt
    # done stays reachable once territory is exhausted, not only when every node is understood.
    assert "already probed twice" in prompt


def test_question_cap_default_override_and_malformed(monkeypatch):
    monkeypatch.delenv("APOLLO_UNIFIED_QUESTION_CAP", raising=False)
    assert unified.question_cap() == 8
    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_CAP", "3")
    assert unified.question_cap() == 3
    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_CAP", "bad")
    assert unified.question_cap() == 8


@pytest.mark.asyncio
async def test_debug_log_flag_reports_belt_verdicts(monkeypatch, caplog):
    monkeypatch.setattr(
        unified,
        "_call_unified",
        lambda **kwargs: json.dumps(_draft(acknowledgement=None, question="What did Toffler say?")),
    )
    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_DEBUG_LOG", "true")
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": "Toffler", "meaning": "private theory"},
            )
        ],
        edges=[],
    )
    with caplog.at_level("INFO"):
        await unified.evaluate_and_ask(
            **_kwargs(
                reference_graph=graph,
                tally_state=(unified.TallyState("a", "Toffler", "missing"),),
            )
        )
    assert "apollo_unified_question_debug" in caplog.text
    assert "belt_hit=True" in caplog.text
    assert "toffler" in caplog.text
    assert "final='What did Toffler say?'" in caplog.text


def test_private_helpers_and_invalid_updates():
    assert unified._walk_strings({"a": [" x ", 2, {"b": ""}], "c": ("y",)}) == ["x", "y"]
    assert unified._validated_evidence("HELLO there", ["Well, hello there!"]) == "HELLO there"
    assert unified._public_question_parts("First? And second? ") == ["First", "And second"]
    decoded = _draft(
        updates=[
            {
                "node_id": "a",
                "status": "understood",
                "evidence": {"turn_id": 0, "quote": "never said"},
                "student_declined": None,
            },
            {"node_id": "unknown", "status": "missing", "evidence": None, "student_declined": None},
            None,
        ]
    )
    assert (
        unified._decode_updates(decoded, valid_ids={"a"}, transcript=[("student", "hello")]) == ()
    )
    assert unified._decode("not json") == {}
    assert unified._decode_updates({"tally_updates": None}, valid_ids={"a"}, transcript=[]) == ()
    assert (
        unified._fallback_public_question(
            public_parts=[], reference_graph=_graph(), tally_state=_state(), updates=()
        )
        == "?"
    )


def test_private_strings_include_edge_labels():
    graph = _graph()
    graph.edges.append(
        Edge(
            edge_type=EdgeType.DEPENDS_ON,
            from_node_id="a",
            to_node_id="b",
            attempt_id=1,
            source="reference",
            from_node_type="definition",
            to_node_type="procedure_step",
        )
    )
    assert "DEPENDS_ON" in unified._private_strings(graph)
