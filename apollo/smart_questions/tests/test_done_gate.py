"""P3.2 L2b — the done-gate: Apollo may not self-declare done while a challenge
is owed.

The gate spends the wrongness signal INSIDE the attempt, where the student can
still fix the claim, at zero grade risk. Every guard here is a separate test
because each one is the difference between "one more question" and "a student
who cannot finish".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer import wrongness
from apollo.smart_questions import challenge, selection, unified


@pytest.fixture(autouse=True)
def _run_mocked_calls_inline(monkeypatch):
    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(unified.asyncio, "to_thread", inline)


def _graph() -> KGGraph:
    """One ungraded definition plus two graded nodes."""
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
            build_node(
                node_type="condition",
                node_id="c",
                attempt_id=1,
                source="reference",
                content={"applies_when": "the surface is flat"},
            ),
        ],
        edges=[],
    )


def _draft(*, action="done", updates=None):
    return json.dumps(
        {
            "tally_updates": updates if updates is not None else [],
            "action": action,
            "target_node_id": None if action == "done" else "b",
            "acknowledgement": None if action == "done" else "Got it.",
            "question": None if action == "done" else "Why does that hold?",
        }
    )


def _state(**statuses):
    """Tally rows for a/b/c; pass ``b=("understood", 0)`` style overrides."""
    rows = []
    for node_id, label in (("a", "pressure"), ("b", "multiply"), ("c", "flat surface")):
        status, asked = statuses.get(node_id, ("missing", 0))
        rows.append(unified.TallyState(node_id, label, status, times_asked=asked))
    return tuple(rows)


def _kwargs(**overrides):
    values = {
        "transcript": [("student", "I multiply by the area every time it is flat")],
        "reference_graph": _graph(),
        "problem": SimpleNamespace(problem_text="Why does pressure work?"),
        "tally_state": _state(),
        "budget": unified.QuestionBudget(questions_asked=0, cap=8),
        "challenge_gate": True,
    }
    values.update(overrides)
    return values


async def _run(monkeypatch, *, raw=None, **overrides):
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: raw or _draft())
    return await unified.evaluate_and_ask(**_kwargs(**overrides))


# --------------------------------------------------------------------------- #
# The two owed shapes                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gate_overrides_self_declared_done_on_material_contradiction(monkeypatch):
    result = await _run(
        monkeypatch,
        # Two student turns, so shape (b) cannot be what fires.
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "I multiply by the area")],
        tally_state=_state(b=("tentative", 1)),
        contested_quotes={"b": "I multiply by the area"},
    )
    assert result.action == "ask"
    assert result.target_node_id == "b"
    assert result.question == (
        'Before I try this on my own — you said "I multiply by the area". '
        "Can you walk me through why that follows?"
    )
    assert result.fallback_served is False


@pytest.mark.asyncio
async def test_gate_overrides_on_understood_zero_ask_single_turn_attempt(monkeypatch):
    """The paragraph dump: one wall of text, everything credited, nothing asked."""
    result = await _run(
        monkeypatch,
        raw=_draft(
            updates=[
                {
                    "node_id": "b",
                    "status": "understood",
                    "evidence": {"turn_id": 0, "quote": "I multiply by the area"},
                }
            ]
        ),
    )
    assert result.action == "ask"
    assert result.target_node_id == "b"
    assert result.question.startswith(challenge.CHALLENGE_PREFIX)


@pytest.mark.asyncio
async def test_gate_does_not_fire_on_a_multi_turn_attempt_without_a_contradiction(monkeypatch):
    result = await _run(
        monkeypatch,
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "because area")],
        tally_state=_state(b=("understood", 0)),
    )
    assert result.action == "done"


@pytest.mark.asyncio
async def test_contradiction_outranks_the_unprobed_claim_shape(monkeypatch):
    result = await _run(
        monkeypatch,
        tally_state=_state(b=("understood", 0), c=("tentative", 0)),
        contested_quotes={"c": "it is always flat"},
    )
    assert result.target_node_id == "c"


@pytest.mark.asyncio
async def test_gate_ignores_ungraded_nodes(monkeypatch):
    """Only graded nodes carry grade risk, so only graded nodes are owed."""
    result = await _run(
        monkeypatch,
        tally_state=_state(a=("understood", 0)),
        contested_quotes={"a": "force divided by area"},
    )
    assert result.action == "done"


# --------------------------------------------------------------------------- #
# The non-negotiable guards                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gate_never_fires_below_level_2(monkeypatch):
    """`challenge_gate=False` is what levels 0 and 1 pass — inert by default."""
    result = await _run(
        monkeypatch,
        challenge_gate=False,
        tally_state=_state(b=("tentative", 1)),
        contested_quotes={"b": "I multiply by the area"},
    )
    assert result.action == "done"
    assert result.target_node_id is None


@pytest.mark.asyncio
async def test_gate_is_inert_with_default_arguments(monkeypatch):
    """The pre-P3.2 call — no gate kwargs at all — still answers done."""
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: _draft())
    values = _kwargs(tally_state=_state(b=("understood", 0)))
    values.pop("challenge_gate")
    assert (await unified.evaluate_and_ask(**values)).action == "done"


@pytest.mark.asyncio
async def test_gate_respects_challenge_budget_of_two(monkeypatch):
    assert challenge.DEFAULT_CHALLENGE_BUDGET == 2
    spent_two = [
        ("student", "one"),
        ("apollo", challenge.render("I multiply by the area")),
        ("student", "two"),
        ("apollo", challenge.CHALLENGE_FALLBACK),
        ("student", "I multiply by the area anyway"),
    ]
    result = await _run(
        monkeypatch,
        transcript=spent_two,
        tally_state=_state(b=("tentative", 1)),
        contested_quotes={"b": "I multiply by the area"},
    )
    assert result.action == "done"


@pytest.mark.asyncio
async def test_gate_still_fires_with_one_challenge_spent(monkeypatch):
    result = await _run(
        monkeypatch,
        transcript=[
            ("student", "one"),
            ("apollo", challenge.render("I multiply by the area")),
            ("student", "I multiply by the area anyway"),
        ],
        tally_state=_state(b=("tentative", 1)),
        contested_quotes={"b": "I multiply by the area"},
    )
    assert result.action == "ask"


@pytest.mark.asyncio
async def test_challenge_budget_env_override_is_clamped(monkeypatch):
    monkeypatch.setenv("APOLLO_CHALLENGE_BUDGET", "0")
    assert challenge.challenge_budget() == 0
    monkeypatch.setenv("APOLLO_CHALLENGE_BUDGET", "-4")
    assert challenge.challenge_budget() == 0
    monkeypatch.setenv("APOLLO_CHALLENGE_BUDGET", "not-a-number")
    assert challenge.challenge_budget() == challenge.DEFAULT_CHALLENGE_BUDGET
    monkeypatch.delenv("APOLLO_CHALLENGE_BUDGET")
    assert challenge.challenge_budget() == challenge.DEFAULT_CHALLENGE_BUDGET


@pytest.mark.asyncio
async def test_a_zero_budget_disables_the_gate(monkeypatch):
    monkeypatch.setenv("APOLLO_CHALLENGE_BUDGET", "0")
    result = await _run(monkeypatch, tally_state=_state(b=("understood", 0)))
    assert result.action == "done"


@pytest.mark.asyncio
async def test_gate_never_exceeds_question_cap(monkeypatch):
    """One question left is still a question; zero left never reaches the gate."""
    at_cap = await _run(
        monkeypatch,
        budget=unified.QuestionBudget(questions_asked=8, cap=8),
        tally_state=_state(b=("understood", 0)),
    )
    assert at_cap.action == "done"
    under_cap = await _run(
        monkeypatch,
        budget=unified.QuestionBudget(questions_asked=7, cap=8),
        tally_state=_state(b=("understood", 0)),
    )
    assert under_cap.action == "ask"


@pytest.mark.asyncio
async def test_gate_unreachable_on_budget_exhausted_branch(monkeypatch):
    """STRUCTURAL: that branch returns before `_call_unified` ever runs, so the
    gate — which rides on the decoded answer — cannot be reached from it."""
    calls: list[dict] = []
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: calls.append(kwargs) or _draft())
    result = await unified.evaluate_and_ask(
        **_kwargs(
            budget=unified.QuestionBudget(questions_asked=8, cap=8),
            tally_state=_state(b=("tentative", 1)),
            contested_quotes={"b": "I multiply by the area"},
        )
    )
    assert calls == []
    assert result.action == "done"
    assert result.tally_updates == ()


@pytest.mark.asyncio
async def test_gate_never_overrides_the_code_enforced_done(monkeypatch):
    """An emptied askable set is the code's own done, not the model's. Only a
    SELF-DECLARED done is challengeable."""
    result = await _run(
        monkeypatch,
        raw=_draft(action="ask"),
        tally_state=_state(a=("understood", 0), b=("understood", 0), c=("understood", 0)),
    )
    assert result.action == "done"


@pytest.mark.asyncio
async def test_gate_target_always_has_an_unspent_ask(monkeypatch):
    """Shape (a) targets come from `policy.askable_ids`, which encodes the cap.
    Shape (b) targets are NOT in that set — `build_selection_policy` drops them
    for being ``understood``, which is the very state the gate challenges — so
    the invariant that actually holds is ``times_asked < MAX_ASKS_PER_NODE``."""
    result = await _run(monkeypatch, tally_state=_state(b=("understood", 0)))
    assert result.target_node_id == "b"
    row = next(item for item in _state(b=("understood", 0)) if item.node_id == "b")
    assert row.times_asked < selection.MAX_ASKS_PER_NODE


@pytest.mark.asyncio
async def test_gate_case_a_target_is_inside_askable_ids(monkeypatch):
    result = await _run(
        monkeypatch,
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "I multiply by the area")],
        tally_state=_state(b=("tentative", 1)),
        contested_quotes={"b": "I multiply by the area"},
    )
    policy = selection.build_selection_policy(
        reference_graph=_graph(),
        tally_state=_state(b=("tentative", 1)),
        questions_asked=0,
        cap=8,
    )
    assert result.target_node_id in policy.askable_ids


@pytest.mark.asyncio
async def test_a_contradiction_at_the_ask_cap_is_not_owed(monkeypatch):
    result = await _run(
        monkeypatch,
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "I multiply by the area")],
        tally_state=_state(b=("conflicting", selection.MAX_ASKS_PER_NODE)),
        contested_quotes={"b": "I multiply by the area"},
    )
    assert result.action == "done"


# --------------------------------------------------------------------------- #
# Shared literals (F-15: the wrongness enum is deliberately duplicated)        #
# --------------------------------------------------------------------------- #


def test_the_gates_string_literals_agree_with_their_authorities():
    """`challenge` is import-light and holds its own copies of two strings that
    already exist elsewhere. Fail-safe direction (a divergence degrades to "no
    challenge"), but a silent no-op gate is exactly what nobody would notice —
    so both copies are pinned to their authority here."""
    assert challenge._MATERIAL == wrongness.WRONGNESS_MATERIAL
    assert challenge._MATERIAL in unified.WRONGNESS_VALUES
    assert challenge._UNDERSTOOD == selection._MASTERED_STATUS


def test_the_two_wrongness_enums_still_agree():
    """`unified.WRONGNESS_VALUES` (producer) and `wrongness.WRONGNESS_VALUES`
    (reader) are duplicated to keep the pure core import-light. Divergence would
    silently retire findings, so it fails the build instead."""
    assert unified.WRONGNESS_VALUES == wrongness.WRONGNESS_VALUES


# --------------------------------------------------------------------------- #
# The template                                                                 #
# --------------------------------------------------------------------------- #


def test_challenge_template_is_deterministic_and_single_question():
    for quote in ("I multiply by the area", "the surface is flat", None, ""):
        rendered = challenge.render(quote)
        assert rendered == challenge.render(quote)
        assert rendered.count("?") == 1
        assert rendered.endswith("?")
    assert challenge.render(None) == challenge.CHALLENGE_FALLBACK


def test_challenge_quote_is_control_stripped_and_length_capped():
    assert challenge.clean_quote("a\x00b\nc  d") == "a b c d"
    assert challenge.clean_quote(None) == ""
    assert challenge.clean_quote(12345) == ""
    long_quote = " ".join(["word"] * 200)
    capped = challenge.clean_quote(long_quote)
    assert len(capped) <= challenge.MAX_QUOTE_CHARS
    assert not capped.endswith("wor")  # cut on a word boundary, not mid-token
    assert challenge.clean_quote("x" * 400) == "x" * challenge.MAX_QUOTE_CHARS


@pytest.mark.asyncio
async def test_challenge_falls_back_when_the_quote_carries_its_own_question_mark(monkeypatch):
    """A second '?' is exactly the malformed shape the belt rejects."""
    result = await _run(
        monkeypatch,
        transcript=[("student", "Is it the area? I multiply by the area")],
        tally_state=_state(b=("understood", 0)),
    )
    assert result.question == challenge.CHALLENGE_FALLBACK


@pytest.mark.asyncio
async def test_challenge_falls_back_when_belt_hits(monkeypatch):
    monkeypatch.setattr(
        unified.challenge,
        "belt_verdict",
        lambda *args, **kwargs: SimpleNamespace(hit=True, malformed=False),
    )
    result = await _run(monkeypatch, tally_state=_state(b=("understood", 0)))
    assert result.question == challenge.CHALLENGE_FALLBACK


def test_challenges_spent_counted_from_transcript_prefix():
    assert challenge.challenges_spent([]) == 0
    assert (
        challenge.challenges_spent(
            [
                ("student", challenge.CHALLENGE_FALLBACK),  # a student echo never counts
                ("apollo", "So how does that work?"),
                ("apollo", challenge.render("I multiply by the area")),
                ("apollo", challenge.CHALLENGE_FALLBACK),
            ]
        )
        == 2
    )


def test_both_renderings_share_the_counted_marker():
    """The fallback has no "you said" clause; counting on the longer prefix
    would leave every fallback challenge uncounted and break the budget cap."""
    assert challenge.CHALLENGE_PREFIX.startswith(challenge.CHALLENGE_MARKER)
    assert challenge.CHALLENGE_FALLBACK.startswith(challenge.CHALLENGE_MARKER)
    assert not challenge.CHALLENGE_FALLBACK.startswith(challenge.CHALLENGE_PREFIX)
    assert challenge.challenges_spent([("apollo", challenge.CHALLENGE_FALLBACK)]) == 1


@pytest.mark.asyncio
async def test_gate_never_calls_the_model_a_second_time(monkeypatch):
    """No extra LLM call, no added latency: the template is code-emitted."""
    calls: list[dict] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return _draft()

    monkeypatch.setattr(unified, "_call_unified", fake_call)
    result = await unified.evaluate_and_ask(**_kwargs(tally_state=_state(b=("understood", 0))))
    assert result.action == "ask"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_gate_preserves_this_turns_tally_updates(monkeypatch):
    result = await _run(
        monkeypatch,
        raw=_draft(
            updates=[
                {
                    "node_id": "b",
                    "status": "understood",
                    "evidence": {"turn_id": 0, "quote": "I multiply by the area"},
                }
            ]
        ),
    )
    assert [item.node_id for item in result.tally_updates] == ["b"]


@pytest.mark.asyncio
async def test_a_clean_update_this_turn_retires_a_ledger_contradiction(monkeypatch):
    """Evidence recency, not the sticky `conflicting` state: a student who
    revised on the newest turn is not challenged for the older claim."""
    result = await _run(
        monkeypatch,
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "I multiply by the area")],
        tally_state=_state(b=("conflicting", 1)),
        contested_quotes={"b": "I divide by the area"},
        raw=_draft(
            updates=[
                {
                    "node_id": "b",
                    "status": "understood",
                    "evidence": {"turn_id": 2, "quote": "I multiply by the area"},
                }
            ]
        ),
    )
    assert result.action == "done"


@pytest.mark.asyncio
async def test_a_fresh_contradiction_this_turn_is_owed_without_any_ledger_read(monkeypatch):
    """The common case: the engine labels a node `contradicts_material` and
    declares done in the SAME answer. The controller could not have seen it —
    it happens inside the call — so the gate reads this turn's updates too."""
    result = await _run(
        monkeypatch,
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "I divide by the area")],
        tally_state=_state(b=("tentative", 1)),
        contested_quotes={},
        raw=_draft(
            updates=[
                {
                    "node_id": "b",
                    "status": "conflicting",
                    "evidence": {"turn_id": 2, "quote": "I divide by the area"},
                    "wrongness": "contradicts_material",
                    "contradiction": {
                        "reference_clause": "multiply by the area",
                        "kind": "inverted relationship",
                    },
                }
            ]
        ),
    )
    assert result.action == "ask"
    assert result.target_node_id == "b"
    assert '"I divide by the area"' in result.question


@pytest.mark.asyncio
async def test_an_update_with_no_evidence_never_retires_a_ledger_contradiction(monkeypatch):
    """A `missing` update appends no evidence entry, so it does not become the
    node's latest — mirroring what `controller._apply_tally_updates` persists."""
    result = await _run(
        monkeypatch,
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "not sure any more")],
        tally_state=_state(b=("conflicting", 1)),
        contested_quotes={"b": "I divide by the area"},
        raw=_draft(updates=[{"node_id": "b", "status": "missing", "evidence": None}]),
    )
    assert result.action == "ask"
    assert result.target_node_id == "b"
