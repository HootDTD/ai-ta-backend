"""The challenge budget is only real if the MARKER survives to the transcript.

`challenge.challenges_spent` is the whole enforcement of
`APOLLO_CHALLENGE_BUDGET`: it re-counts spent challenges out of the attempt
transcript instead of storing them, which is what makes the cap need no column,
no migration, and reset correctly on P0.2's restart wipe. That trade only holds
while the served text reaches `TutoringMessage.content` with
`CHALLENGE_MARKER` still at the FRONT.

Three hops sit between the gate and the row, and none of them is owned by the
questioning slice:

    challenge.resolve -> UnifiedQuestionResult.reply
                      -> QuestionDecision.question        (controller)
                      -> chat's `validated`               (handlers/chat)
                      -> _persist_apollo_reply(content=)  (handlers/chat)

A prefix, a sanitiser, or a rename anywhere along it would silently un-count
every challenge and let the gate fire past its budget forever. Nothing else
crosses that whole chain, so it is pinned here.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from apollo.smart_questions import challenge, controller

pytestmark = pytest.mark.unit


def test_both_renderings_are_counted_after_the_chat_fallback_is_applied() -> None:
    """`chat.py` serves `decision.question or "<generic re-ask>"`. Both challenge
    renderings are truthy, so the `or` can never swap one out — and the generic
    fallback is deliberately NOT counted, because it is not a challenge."""
    generic = "Can you explain that part one more time?"
    for reply in (challenge.render("a quoted claim"), challenge.render(None)):
        assert reply, "an empty reply would be replaced by the generic re-ask"
        assert challenge.challenges_spent([("apollo", reply or generic)]) == 1
    assert challenge.challenges_spent([("apollo", generic)]) == 0


def test_the_counted_marker_survives_a_normalization_round_trip() -> None:
    """`challenges_spent` compares on `leakage.normalized`, so a re-wrapped or
    re-spaced row still counts — but a PREFIXED one deliberately does not, which
    is exactly the regression this file exists to catch."""
    reply = challenge.render("a quoted claim")
    assert challenge.challenges_spent([("apollo", f"  {reply}\n")]) == 1
    assert challenge.challenges_spent([("apollo", f"Quick thought — {reply}")]) == 0
    # Role matters: a student echoing the text never spends the budget.
    assert challenge.challenges_spent([("student", reply)]) == 0


def test_the_controller_returns_the_gate_reply_unmodified() -> None:
    """`plan_next_question`'s ask branch forwards `result.reply` verbatim into
    `QuestionDecision.question`. Read off the source so the assertion cannot be
    satisfied by a stub that happens to agree."""
    tree = ast.parse(inspect.getsource(controller.plan_next_question))
    ask_returns = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
        for kw in node.value.keywords
        if kw.arg == "action" and isinstance(kw.value, ast.Constant) and kw.value.value == "ask"
    ]
    assert len(ask_returns) == 1
    question: ast.expr = next(kw.value for kw in ask_returns[0].keywords if kw.arg == "question")
    # `cast(str, result.reply)` — unwrap the typing cast, then assert the source.
    if isinstance(question, ast.Call):
        question = question.args[-1]
    assert isinstance(question, ast.Attribute) and question.attr == "reply"


def test_chat_persists_the_served_question_verbatim() -> None:
    """The last hop: `chat.py` hands `validated` straight to
    `_persist_apollo_reply(apollo_msg=…)`, which stores it as `content` with no
    transformation. A sanitiser or a prefix inserted here is the failure this
    pins — `sanitize_narrative` belongs to the at-Done feedback lane, never to a
    served question."""
    source = Path(controller.__file__).resolve().parents[1] / "handlers" / "chat.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_persist_apollo_reply"
    ]
    assert calls, "chat.py must still persist Apollo's reply through this helper"
    for call in calls:
        msg = next(kw.value for kw in call.keywords if kw.arg == "apollo_msg")
        assert isinstance(msg, ast.Name), ast.dump(msg)
        assert msg.id == "validated"

    persist = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_persist_apollo_reply"
    )
    content = [
        kw.value
        for node in ast.walk(persist)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "content"
    ]
    assert len(content) == 1
    assert isinstance(content[0], ast.Name) and content[0].id == "apollo_msg"
