import json
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


def _payload(
    *,
    action="ask",
    target="a",
    part=0,
    acknowledgement="I understand that you use pressure.",
    question="Why does your pressure step work?",
    clause_coverage=None,
):
    return {
        "nodes": [
            {
                "node_id": "a",
                "state": "tentative",
                "credit": 0.4,
                "student_evidence": "I use pressure",
            },
            {
                "node_id": "b",
                "state": "missing",
                "credit": 0.0,
                "student_evidence": None,
            },
        ],
        "public_clause_coverage": clause_coverage or [{"index": 0, "status": "unattempted"}],
        "action": action,
        "target_node_id": target,
        "public_question_part_index": part,
        "acknowledgement": acknowledgement,
        "question": question,
    }


@pytest.mark.asyncio
async def test_one_call_returns_evidence_backed_tally_and_question(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs["payload"])
        return json.dumps(_payload())

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    history = (unified.QuestionHistory("b", "What happens next?", "answered"),)
    result = await unified.evaluate_and_ask(
        transcript=[("student", "I use pressure here")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Why does pressure work?"),
        question_history=history,
    )

    assert len(calls) == 1
    assert calls[0]["public_question_parts"] == [{"index": 0, "text": "Why does pressure work"}]
    assert calls[0]["question_history"][0]["node_id"] == "b"
    assert result.action == "ask"
    assert result.target_node_id == "a"
    assert result.reply == "I understand that you use pressure. Why does your pressure step work?"
    assert result.coverage[0] == unified.NodeCoverage("a", "tentative", 0.4, "I use pressure")


@pytest.mark.asyncio
async def test_unverifiable_evidence_cannot_create_learned_state(monkeypatch):
    payload = _payload()
    payload["nodes"][0] = {
        "node_id": "a",
        "state": "understood",
        "credit": 7,
        "student_evidence": "words the student never said",
    }
    payload["nodes"].extend([None, {"node_id": "invented", "state": "understood"}])
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: json.dumps(payload))
    result = await unified.evaluate_and_ask(
        transcript=[("student", "I use pressure")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain pressure?"),
        question_history=(),
    )
    assert result.coverage[0] == unified.NodeCoverage("a", "missing", 0.0, None)


@pytest.mark.asyncio
async def test_all_nodes_understood_finishes_even_if_model_requests_question(monkeypatch, caplog):
    payload = _payload()
    payload["nodes"] = [
        {
            "node_id": "a",
            "state": "understood",
            "credit": 1,
            "student_evidence": "pressure",
        },
        {
            "node_id": "b",
            "state": "understood",
            "credit": 0.8,
            "student_evidence": "area",
        },
    ]
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: json.dumps(payload))
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            transcript=[("student", "pressure and area")],
            reference_graph=_graph(),
            problem=SimpleNamespace(problem_text="Explain pressure?"),
            question_history=(),
        )
    assert result.action == "done"
    assert result.reply is None
    assert "action=done" in caplog.text


@pytest.mark.asyncio
async def test_malformed_output_defaults_to_missing_and_public_question(monkeypatch):
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: "not json")
    malformed = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Explain the process."),
        question_history=(),
    )
    assert all(item.state == "missing" for item in malformed.coverage)
    assert malformed.target_node_id == "a"
    assert malformed.reply == "Explain the process?"

    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: "[]")
    wrong_shape = await unified.evaluate_and_ask(
        transcript=[],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text=""),
        question_history=(),
    )
    assert wrong_shape.reply == unified._GENERIC_FALLBACK


@pytest.mark.asyncio
async def test_bad_credit_and_invalid_target_fail_safe(monkeypatch):
    payload = _payload(target="invented", acknowledgement=None, question=42)
    payload["nodes"] = [
        {
            "node_id": "a",
            "state": "tentative",
            "credit": "bad",
            "student_evidence": "pressure",
        }
    ]
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: json.dumps(payload))
    result = await unified.evaluate_and_ask(
        transcript=[("student", "pressure")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="What is pressure?"),
        question_history=(),
    )
    assert result.target_node_id == "a"
    assert result.coverage[0].credit == 0
    assert result.reply == "What is pressure?"


@pytest.mark.asyncio
async def test_future_shock_private_paraphrase_is_rejected_for_public_gap(monkeypatch):
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="cause",
                attempt_id=1,
                source="reference",
                content={
                    "concept": "choice overload",
                    "meaning": "too many available paths create decision paralysis",
                },
            )
        ],
        edges=[],
    )
    payload = {
        "nodes": [
            {
                "node_id": "cause",
                "state": "tentative",
                "credit": 0.4,
                "student_evidence": "it is overwealming",
            }
        ],
        "public_clause_coverage": [
            {"index": 0, "status": "attempted"},
            {"index": 1, "status": "unattempted"},
            {"index": 2, "status": "unattempted"},
        ],
        "action": "ask",
        "target_node_id": "cause",
        "public_question_part_index": 1,
        "acknowledgement": (
            "Things are happening too quickly and it becomes difficult to keep up."
        ),
        "question": ("How do different paths to choose from make the future feel overwhelming?"),
    }
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: json.dumps(payload))
    result = await unified.evaluate_and_ask(
        transcript=[
            (
                "student",
                "future shock occurs when things are happening too quickly and it becomes difficult to keep up",
            ),
            ("apollo", "Can you explain more?"),
            ("student", "it is overwealming"),
        ],
        reference_graph=graph,
        problem=SimpleNamespace(
            problem_text=(
                "What is Future Shock, and why does it occur? When did it start happening — can "
                "you give an example? And is it still happening today — why or why not?"
            )
        ),
        question_history=(),
    )
    assert result.reply == "When did it start happening — can you give an example?"
    assert "paths" not in result.reply
    assert "overwealming" not in result.reply


@pytest.mark.asyncio
async def test_future_shock_does_not_repeat_attempted_public_clause(monkeypatch):
    payload = _payload(
        target="a",
        part=0,
        acknowledgement=None,
        question="What is Future Shock, and why does it occur?",
        clause_coverage=[
            {"index": 0, "status": "attempted"},
            {"index": 1, "status": "unattempted"},
            {"index": 2, "status": "unattempted"},
        ],
    )
    payload["nodes"][0]["student_evidence"] = (
        "future shock occurs when things are happening too quickly"
    )
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: json.dumps(payload))
    result = await unified.evaluate_and_ask(
        transcript=[
            (
                "student",
                "future shock occurs when things are happening too quickly and it becomes difficult to keep up",
            )
        ],
        reference_graph=_graph(),
        problem=SimpleNamespace(
            problem_text=(
                "What is Future Shock, and why does it occur? When did it start happening — can "
                "you give an example? And is it still happening today — why or why not?"
            )
        ),
        question_history=(),
    )
    assert result.reply == "When did it start happening — can you give an example?"


@pytest.mark.asyncio
async def test_clause_coverage_advances_after_two_student_attempts(monkeypatch):
    payload = _payload(
        target="a",
        part=0,
        acknowledgement=None,
        question="What is Future Shock, and why does it occur?",
        clause_coverage=[
            {"index": 0, "status": "attempted"},
            {"index": 1, "status": "answered"},
            {"index": 2, "status": "unattempted"},
        ],
    )
    payload["nodes"][0]["student_evidence"] = (
        "future shock occurs when things are happening too quickly"
    )
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: json.dumps(payload))

    result = await unified.evaluate_and_ask(
        transcript=[
            (
                "student",
                "future shock occurs when things are happening too quickly and it becomes difficult to keep up",
            ),
            ("apollo", "When did it start happening -- can you give an example?"),
            ("student", "it started happening in 1970"),
        ],
        reference_graph=_graph(),
        problem=SimpleNamespace(
            problem_text=(
                "What is Future Shock, and why does it occur? When did it start happening -- can "
                "you give an example? And is it still happening today -- why or why not?"
            )
        ),
        question_history=(),
    )

    assert result.reply == "is it still happening today -- why or why not?"
    assert result.question == "is it still happening today -- why or why not?"


@pytest.mark.asyncio
async def test_transcript_dedup_remembers_probe_after_ledger_overwrite(monkeypatch):
    rejected = _payload(
        acknowledgement=None,
        question="Where do quasar tachyons connect?",
        clause_coverage=[{"index": 0, "status": "attempted"}],
    )
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(rejected)

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    result = await unified.evaluate_and_ask(
        transcript=[
            ("student", "I use pressure"),
            ("apollo", "What makes that happen?"),
            ("student", "It just does"),
            ("apollo", "What happens next?"),
            ("student", "I am not sure"),
        ],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Why does pressure happen?"),
        question_history=(unified.QuestionHistory("a", "What happens next?", "missing"),),
    )

    assert len(calls) == 2
    assert result.question == unified._GENERIC_FALLBACK
    assert result.question != "What makes that happen?"


@pytest.mark.asyncio
async def test_rejected_draft_retries_once_and_recovers(monkeypatch, caplog):
    drafts = [
        _payload(acknowledgement=None, question="Where do quasar tachyons connect?"),
        _payload(acknowledgement=None, question="What should I understand next?"),
    ]
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(drafts[len(calls) - 1])

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            transcript=[("student", "I use pressure")],
            reference_graph=_graph(),
            problem=SimpleNamespace(problem_text="Why does pressure work?"),
            question_history=(),
        )

    assert len(calls) == 2
    assert result.reply == "What should I understand next?"
    assert calls[1]["messages"][0] is calls[0]["messages"][0]
    assert calls[1]["messages"][1] is calls[0]["messages"][1]
    assert calls[1]["messages"][:2] == calls[0]["messages"]
    assert calls[1]["messages"] == [
        *calls[0]["messages"],
        {"role": "assistant", "content": json.dumps(drafts[0])},
        calls[1]["messages"][3],
    ]
    assert "tachyons" in calls[1]["messages"][3]["content"]
    assert "question_vocabulary_boundary_retry_recovered" in caplog.text


@pytest.mark.asyncio
async def test_second_rejection_uses_canned_fallback_with_only_two_calls(monkeypatch, caplog):
    rejected = _payload(acknowledgement=None, question="Where do quasar tachyons connect?")
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(rejected)

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            transcript=[("student", "I use pressure")],
            reference_graph=_graph(),
            problem=SimpleNamespace(problem_text="Why does pressure work?"),
            question_history=(),
        )

    assert len(calls) == 2
    assert result.question == "Why does pressure work?"
    assert "question_vocabulary_boundary_retry_failed" in caplog.text


@pytest.mark.asyncio
async def test_repeat_retry_feedback_lists_all_asked_questions(monkeypatch):
    repeated = _payload(acknowledgement=None, question="What happens next?")
    recovered = _payload(acknowledgement=None, question="What should I understand next?")
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(repeated if len(calls) == 1 else recovered)

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    await unified.evaluate_and_ask(
        transcript=[
            ("student", "I use pressure"),
            ("apollo", "I follow. What happens next?"),
            ("student", "I am not sure"),
        ],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Why does pressure work?"),
        question_history=(unified.QuestionHistory("b", "Why is that?", "missing"),),
    )

    feedback = calls[1]["messages"][3]["content"]
    assert "A NEW question is required" in feedback
    assert "What happens next?" in feedback
    assert "Why is that?" in feedback


def test_apollo_vocabulary_is_safe_for_question_but_not_acknowledgement():
    common = {
        "reference_graph": _graph(),
        "public_text": "Explain pressure?",
        "student_messages": ["pressure"],
        "apollo_messages": ["We talked about quasiparticles."],
        "prior_questions": [],
        "public_parts": ["Explain pressure"],
    }
    safe_question = unified._validate_draft(
        acknowledgement=None,
        question="How do quasiparticles connect?",
        **common,
    )
    unsafe_ack = unified._validate_draft(
        acknowledgement="Quasiparticles connect.",
        question="How do quasiparticles connect?",
        **common,
    )

    assert safe_question.reason is None
    assert unsafe_ack.reason == "unsafe_acknowledgement"


def test_live_future_shock_question_and_acknowledgement_are_safe():
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="future-shock",
                attempt_id=1,
                source="reference",
                content={
                    "concept": "Future Shock",
                    "meaning": "rapid social and technological change described by Toffler in 1970",
                },
            )
        ],
        edges=[],
    )
    validation = unified._validate_draft(
        acknowledgement=(
            "So far you've basically defined Future Shock as when things are happening too quickly…"
        ),
        question=(
            "What's one period when you think this 'too quickly to keep up' feeling started "
            "happening, and what's one example of the kind of change from that time?"
        ),
        reference_graph=graph,
        public_text=(
            "What is Future Shock, and why does it occur? When did it start happening — can you "
            "give an example? And is it still happening today — why or why not?"
        ),
        student_messages=[
            "Future Shock means life can feel too quick for people.",
            "Things keep happening faster and quickly become hard to keep up with.",
        ],
        apollo_messages=[],
        prior_questions=[],
        public_parts=[],
    )

    assert validation.reason is None
    assert validation.acknowledgement.startswith("So far you've basically defined")


def test_common_words_do_not_weaken_digits_proper_nouns_or_private_phrases():
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="private",
                attempt_id=1,
                source="reference",
                content={"concept": "private concept", "meaning": "change over time"},
            )
        ],
        edges=[],
    )
    common = {
        "reference_graph": graph,
        "public_text": "Why does it occur?",
        "student_messages": [],
    }

    assert unified._leaks_private_content("Did it begin in 1970?", **common)
    assert unified._leaks_private_content("What did Toffler say?", **common)
    assert unified._leaks_private_content("How does change over time happen?", **common)


def test_acknowledgement_still_cannot_assert_public_problem_vocabulary():
    validation = unified._validate_draft(
        acknowledgement="Photosynthesis converts light.",
        question="What is your idea?",
        reference_graph=KGGraph(nodes=[], edges=[]),
        public_text="How does photosynthesis convert light?",
        student_messages=["I have an idea."],
        apollo_messages=[],
        prior_questions=[],
        public_parts=[],
    )

    assert validation.reason == "unsafe_acknowledgement"
    assert validation.acknowledgement == ""


def test_safe_token_match_uses_light_morphology_without_short_stem_overmatch():
    assert unified._safe_token_match("started", {"start"})
    assert unified._safe_token_match("occurs", {"occur"})
    assert not unified._safe_token_match("gas", {"ga"})


def test_exhausted_canned_repertoire_rotates_away_from_previous_question():
    parts = ["Why does alpha happen", "Can you give an example"]
    repertoire = unified._canned_repertoire(parts)
    prior = [*repertoire, repertoire[-1]]

    selected = unified._fallback_question(
        parts,
        0,
        prior,
        clause_statuses=("answered", "answered"),
    )

    assert selected != repertoire[-1]
    assert selected in repertoire


@pytest.mark.asyncio
async def test_debug_cycle_log_is_default_off_and_flag_gated(monkeypatch, caplog):
    drafts = [
        _payload(acknowledgement=None, question="Where do quasar tachyons connect?"),
        _payload(acknowledgement=None, question="What should I understand next?"),
    ]
    call_count = 0

    def fake_call(**kwargs):
        nonlocal call_count
        draft = drafts[call_count % len(drafts)]
        call_count += 1
        return json.dumps(draft)

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    kwargs = {
        "transcript": [("student", "I use pressure")],
        "reference_graph": _graph(),
        "problem": SimpleNamespace(problem_text="Why does pressure work?"),
        "question_history": (),
    }
    monkeypatch.delenv("APOLLO_UNIFIED_QUESTION_DEBUG_LOG", raising=False)
    with caplog.at_level("INFO"):
        await unified.evaluate_and_ask(**kwargs)
    assert "apollo_unified_question_debug" not in caplog.text

    caplog.clear()
    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_DEBUG_LOG", "true")
    with caplog.at_level("INFO"):
        await unified.evaluate_and_ask(**kwargs)
    assert caplog.text.count("apollo_unified_question_debug") == 1
    assert "draft_question='Where do quasar tachyons connect?'" in caplog.text
    assert "draft_rejection=question_vocabulary_boundary" in caplog.text
    assert "draft_offending_tokens=quasar, tachyons" in caplog.text
    assert "redraft_question='What should I understand next?'" in caplog.text
    assert "redraft_validation=accepted" in caplog.text
    assert "final_question='What should I understand next?'" in caplog.text


@pytest.mark.asyncio
async def test_retry_question_recovery_is_honest_when_only_ack_is_dropped(monkeypatch, caplog):
    drafts = [
        _payload(acknowledgement=None, question="Where do quasar tachyons connect?"),
        _payload(
            acknowledgement="Do I understand?",
            question="What should I understand next?",
        ),
    ]
    calls = 0

    def fake_call(**kwargs):
        nonlocal calls
        draft = drafts[calls]
        calls += 1
        return json.dumps(draft)

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_DEBUG_LOG", "true")
    with caplog.at_level("INFO"):
        result = await unified.evaluate_and_ask(
            transcript=[("student", "I use pressure")],
            reference_graph=_graph(),
            problem=SimpleNamespace(problem_text="Why does pressure work?"),
            question_history=(),
        )

    assert result.reply == "What should I understand next?"
    assert "question_vocabulary_boundary_retry_recovered" in caplog.text
    assert "redraft_validation=unsafe_acknowledgement" in caplog.text


def test_debug_tokens_are_bounded():
    validation = unified._DraftValidation(
        acknowledgement="",
        question="",
        reason="question_vocabulary_boundary",
        offending_tokens=("private" * 100,),
    )

    assert len(unified._bounded_debug_tokens(validation)) == 300


@pytest.mark.asyncio
async def test_payload_orders_stable_fields_before_turn_varying_fields(monkeypatch):
    calls = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return json.dumps(_payload())

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    await unified.evaluate_and_ask(
        transcript=[("student", "I use pressure")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Why does pressure work?"),
        question_history=(),
    )

    assert list(calls[0]["payload"]) == [
        "public_problem",
        "public_question_parts",
        "private_reference_nodes",
        "private_reference_edges",
        "question_history",
        "transcript",
    ]


def test_clause_aware_fallback_redirects_answered_and_narrows_attempted():
    parts = ["What is alpha", "Why does beta happen", "How does gamma work"]
    assert (
        unified._fallback_question(
            parts,
            0,
            clause_statuses=("answered", "unattempted", "unattempted"),
        )
        == "Why does beta happen?"
    )
    narrowed = unified._fallback_question(
        parts,
        0,
        clause_statuses=("answered", "attempted", "answered"),
    )
    assert narrowed == "What makes that happen?"
    assert narrowed != "Why does beta happen?"
    assert (
        unified._fallback_question(
            ["Why does beta happen"],
            0,
            avoid_index=0,
            clause_statuses=("attempted",),
        )
        == "What makes that happen?"
    )
    assert (
        unified._fallback_question(
            ["Why does beta happen"],
            0,
            avoid_index=0,
            clause_statuses=("unattempted",),
        )
        == "What makes that happen?"
    )
    assert (
        unified._fallback_question(
            parts,
            0,
            clause_statuses=("answered", "answered", "answered"),
        )
        == unified._GENERIC_FALLBACK
    )


def test_invalid_clause_coverage_defaults_each_clause_to_unattempted():
    assert unified._clause_statuses({"public_clause_coverage": None}, 2) == (
        "unattempted",
        "unattempted",
    )
    assert unified._clause_statuses(
        {
            "public_clause_coverage": [
                {"index": 0, "status": "answered"},
                {"index": True, "status": "attempted"},
                {"index": 1, "status": "invalid"},
                {"index": 99, "status": "answered"},
                None,
            ]
        },
        3,
    ) == ("answered", "unattempted", "unattempted")


def test_safe_reply_rejects_echo_repeat_bad_ack_and_direct_private_leak():
    graph = _graph()
    common = {
        "fallback": "Why does pressure work?",
        "reference_graph": graph,
        "public_text": "Why does pressure work?",
        "student_messages": ["pressure works in this pressure step"],
        "prior_questions": [],
    }
    reply, question, reason = unified._safe_reply(
        acknowledgement=None,
        question="pressure works in this pressure step?",
        **common,
    )
    assert (reply, question, reason) == (
        "Why does pressure work?",
        "Why does pressure work?",
        "question_echo",
    )

    reply, question, reason = unified._safe_reply(
        acknowledgement="Do I understand?",
        question="Why does pressure work?",
        **{**common, "prior_questions": ["Why does pressure work?"]},
    )
    assert reply == "Why does pressure work?"
    assert question == "Why does pressure work?"
    assert reason == "repeated_question"

    reply, question, reason = unified._safe_reply(
        acknowledgement="Force divided by area.",
        question="Why does pressure work?",
        **common,
    )
    assert reply == "Why does pressure work?"
    assert question == "Why does pressure work?"
    assert reason == "unsafe_acknowledgement"


def test_private_helpers_cover_nested_content_spelling_and_part_selection():
    assert unified._walk_strings({"a": [" x ", 2, {"b": ""}], "c": ("y",)}) == ["x", "y"]
    assert unified._validated_evidence(None, ["hello"]) is None
    assert unified._validated_evidence("!!!", ["!!!"]) is None
    assert unified._validated_evidence("HELLO there", ["Well, hello there!"]) == "HELLO there"
    assert unified._public_question_parts("First? And second? ") == ["First", "And second"]
    assert unified._fallback_question(["First", "And second"], 1) == "second?"
    assert unified._fallback_question(["First"], 20) == "First?"
    assert unified._fallback_question(["First", "And second"], 0, ["First?"]) == "second?"
    assert unified._fallback_question(["First"], 0, ["First?"]) == unified._GENERIC_FALLBACK
    assert unified._fallback_question([], None) == unified._GENERIC_FALLBACK
    assert unified._fallback_question(["Why does it occur"], 0, avoid_index=0) == (
        "What makes that happen?"
    )
    assert unified._narrow_generic_probe("Can you give an example") == (
        "Can you give a concrete example?"
    )
    assert unified._narrow_generic_probe("When did this start") == "When does that happen?"
    assert unified._narrow_generic_probe("How does it work") == "How do those steps connect?"
    assert not unified._leaks_private_content(
        "Why is it overwhelming?",
        reference_graph=_graph(),
        public_text="Why is it overwhelming?",
        student_messages=["It is overwealming"],
    )
    assert unified._leaks_private_content(
        "Could you explain force divided by area?",
        reference_graph=_graph(),
        public_text="Explain pressure?",
        student_messages=["pressure"],
    )


def test_call_uses_gpt_5_2_medium_reasoning_and_strict_output(monkeypatch):
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
    assert captured["model"] == "gpt-5.2"
    assert captured["reasoning_effort"] == "medium"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert (
        "public_clause_coverage" in captured["response_format"]["json_schema"]["schema"]["required"]
    )
    assert "Recompute the entire tally" in captured["messages"][0]["content"]
    assert "future shock" not in captured["messages"][0]["content"].casefold()
    assert "temperature" not in captured


def test_call_omits_reasoning_effort_for_non_reasoning_override(monkeypatch):
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))])

    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_MODEL", "gpt-4o")
    monkeypatch.setenv("APOLLO_UNIFIED_QUESTION_REASONING_EFFORT", "high")
    monkeypatch.setattr(
        unified,
        "OpenAI",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )
    assert unified._call_unified(payload={}) == "{}"
    assert "reasoning_effort" not in captured
