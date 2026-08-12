"""G9 — the done-gate replayed against the four committed prod fixtures.

**This measures GATING, not grade movement (§4.1).** It asserts which recorded
attempts would have been asked one more question, and nothing whatsoever about
what any of them would then have scored. The gate is NOT claimed to kill
attempt 83 — only a live cohort can measure that, and the PR body says so.

Every fixture is replayed through the real `evaluate_and_ask` with a stubbed
client returning the recorded ``action: "done"`` (the S3 seam — no network, no
model), so what runs here is the shipped decode, policy and gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apollo.schemas.problem import Problem
from apollo.smart_questions import challenge, controller, unified

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "campaign" / "fixtures" / "turn_replay"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


class _StubClient:
    """The recorded end-of-attempt answer: `done`, no new tally updates."""

    def __init__(self) -> None:
        self.calls = 0
        payload = json.dumps(
            {
                "tally_updates": [],
                "action": "done",
                "target_node_id": None,
                "acknowledgement": None,
                "question": None,
            }
        )
        message = SimpleNamespace(content=payload)
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create(response)))

    def _create(self, response):
        def create(**kwargs):
            self.calls += 1
            return response

        return create


def _ledger_row(entry: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        reference_node_id=entry["reference_node_id"],
        state=entry["state"],
        evidence=entry["evidence"],
        times_asked=entry["times_asked"],
        last_asked_turn=entry["last_asked_turn"],
    )


async def _replay(name: str, *, challenge_gate: bool = True):
    """One turn of the recorded attempt, at its recorded end state."""
    fixture = _fixture(name)
    problem = Problem.model_validate(fixture["problem"])
    reference_graph = problem.to_kg_graph(1)
    rows = [_ledger_row(entry) for entry in fixture["question_opportunities"]]
    tally_state = controller._build_tally_state(reference_graph, rows)
    messages = fixture["messages"]
    # Decision time is BEFORE Apollo's closing turn, so drop a trailing apollo
    # message — it is the reply this call would have produced.
    if messages and messages[-1]["role"] == "apollo":
        messages = messages[:-1]
    transcript = [(item["role"], item["content"]) for item in messages]
    client = _StubClient()
    result = await unified.evaluate_and_ask(
        transcript=transcript,
        reference_graph=reference_graph,
        problem=problem,
        tally_state=tally_state,
        budget=unified.QuestionBudget(
            questions_asked=sum(row.times_asked for row in rows),
            cap=unified.question_cap(),
        ),
        client=client,
        challenge_gate=challenge_gate,
    )
    return result, client, tally_state


@pytest.mark.asyncio
async def test_gate_fires_on_the_paragraph_dump(monkeypatch):
    """Attempt 83: one wall of text, a graded node marked `understood` with
    `times_asked == 0`, and a self-declared done. GATING ONLY — this asserts one
    more question was owed, never that the recorded A+ would have changed."""
    result, client, _ = await _replay("attempt_083_paragraph_dump")
    assert result.action == "ask"
    assert result.target_node_id == "q2_four_impairments"  # the graded `condition`
    assert result.question.startswith(challenge.CHALLENGE_MARKER)
    assert client.calls == 1  # no extra model call, no added latency


@pytest.mark.asyncio
async def test_the_same_attempt_is_untouched_below_level_2(monkeypatch):
    result, _, _ = await _replay("attempt_083_paragraph_dump", challenge_gate=False)
    assert result.action == "done"
    assert result.target_node_id is None


@pytest.mark.asyncio
async def test_gate_does_not_fire_on_the_self_correction_attempt(monkeypatch):
    """Attempt 167 is the false-positive regression: a student who self-corrected
    after two probes. Six student turns kill shape (b); the graded node sits at
    the ask cap, which kills shape (a)."""
    result, _, _ = await _replay("attempt_167_self_correction")
    assert result.action == "done"


@pytest.mark.asyncio
async def test_gate_does_not_fire_on_the_conflicting_graded_attempt(monkeypatch):
    """Attempt 124 argued its way to `conflicting` across eight turns and spent
    every ask. There is nothing left to owe — the gate never creates a third ask."""
    result, _, _ = await _replay("attempt_124_conflicting_graded")
    assert result.action == "done"


@pytest.mark.asyncio
async def test_gate_does_not_manufacture_a_challenge_on_the_empty_attempt(monkeypatch):
    """Attempt 86 (defect I7): zero student messages, credited nodes anyway. The
    empty-attempt guard is P0.1's job at Done; the gate must not paper over it
    with a question about a conversation that never happened."""
    result, _, _ = await _replay("attempt_086_zero_transcript")
    assert result.action == "done"


@pytest.mark.asyncio
async def test_exactly_one_of_the_four_fixtures_is_gated(monkeypatch):
    gated = []
    for name in (
        "attempt_083_paragraph_dump",
        "attempt_086_zero_transcript",
        "attempt_124_conflicting_graded",
        "attempt_167_self_correction",
    ):
        result, _, _ = await _replay(name)
        if result.action == "ask":
            gated.append(name)
    assert gated == ["attempt_083_paragraph_dump"]
