"""§4 gates for LEVEL 2 — scheduling. One test per gate ID.

| Gate | Spec §4 row | Threshold | Asserted here by |
|---|---|---|---|
| **G8** | Consent: student-initiated Done never blocked; ``budget_exhausted`` branch never gated | 0 violations, deterministic test | `test_gate_G8_*` |
| **G9** | Gate coverage on attempt 83's cohort (1-turn, 0-ask, >=97) | all gated or individually explained — **measures gating, not grade movement (§4.1)** | `test_gate_G9_*` |

**G9 MEASURES GATING, NOT GRADE MOVEMENT — §4.1, restated because it is the one
claim this build is most likely to be misquoted on.** ``turn_replay`` feeds a
RECORDED transcript's student turns through ``evaluate_and_ask``; it cannot
generate a student's answer to a question that was never asked. Attempt 83's
single message is a polished, correct, 1,964-character essay, so forcing one more
question most plausibly yields another polished correct answer and the same A+.
**Nothing here supports the claim that the gate "kills attempt 83."** The
substitute evidence for any such claim is named in §4.1: a live staging cohort at
level 2, a human read of every forced-question turn and the answer it drew, and
repetition on the pilot concept after promotion.

**A second honest limitation of the playback matrix**, recorded in
``campaign/turn-replay.md`` and re-stated by a test below: prod ran pre-P3.2, so
``reconstruct_producer_responses`` rebuilds every replayed update as
``wrongness: "none"`` and gate shape **(a)** — the contradiction shape — can
never fire offline. Only shape **(b)**, which is ``state``-based, is observable
in playback. A wrongness-bearing arm needs a LIVE draw first (>= 4 samples).

Underlying slice evidence: ``apollo/smart_questions/tests/test_done_gate.py``,
``.../test_done_gate_consent.py``, ``.../test_done_gate_g9_cohort.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apollo.handlers import chat, done
from apollo.ontology import KGGraph, build_node
from apollo.smart_questions import challenge, controller, unified
from campaign import turn_replay

pytestmark = pytest.mark.unit

_QUESTIONING_ENTRY_POINTS = {"plan_next_question", "evaluate_and_ask", "resolve"}


# --------------------------------------------------------------------------- #
# G8 — consent                                                                 #
# --------------------------------------------------------------------------- #


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = child.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _function(module, name: str) -> ast.AST:
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module.__name__}.{name} not found")


def test_gate_G8_the_scanner_detects_a_call():
    """Control for the structural halves below: a scanner that finds nothing
    would pass every one of them for the wrong reason."""
    source = ast.parse("async def f():\n    await plan_next_question(db)\n    await m.resolve()\n")

    assert _called_names(source) == {"plan_next_question", "resolve"}


def test_gate_G8_student_initiated_done_is_structurally_unblockable():
    """P0.4 consent outranks anti-gaming, and it is structural rather than
    conditional: a student reaches grading through ``POST /done`` ->
    ``handle_done`` or an affirmed ``done`` intent ->
    ``chat._handle_pending_done`` -> ``handle_done``, and NEITHER path calls the
    questioning engine. The only ``done`` the gate can override is one Apollo
    decided for itself."""
    assert _called_names(_function(done, "handle_done")).isdisjoint(_QUESTIONING_ENTRY_POINTS)
    handled = _called_names(_function(chat, "_handle_pending_done"))
    assert handled.isdisjoint(_QUESTIONING_ENTRY_POINTS)
    assert "handle_done" in handled
    for module in (unified, controller, challenge):
        source = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        assert "handle_done" not in _called_names(source)
        assert "_grade_claimed_attempt" not in _called_names(source)


def _graph() -> KGGraph:
    return KGGraph(
        nodes=[
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


def _draft() -> str:
    return json.dumps(
        {
            "tally_updates": [],
            "action": "done",
            "target_node_id": None,
            "acknowledgement": None,
            "question": None,
        }
    )


async def test_gate_G8_the_budget_exhausted_branch_is_never_gated(monkeypatch):
    """That branch returns at the TOP of ``evaluate_and_ask``, before
    ``_decode_updates`` runs, so the gate — which rides the decoded answer —
    is structurally unreachable from it. Asserted by proving the producer was
    never even called: a student out of questions is never handed one more."""

    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(unified.asyncio, "to_thread", inline)
    calls: list[dict] = []
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: calls.append(kwargs) or _draft())

    result = await unified.evaluate_and_ask(
        transcript=[("student", "I multiply by the area every time it is flat")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Why does pressure work?"),
        tally_state=(unified.TallyState("b", "multiply", "tentative", times_asked=1),),
        budget=unified.QuestionBudget(questions_asked=8, cap=8),
        challenge_gate=True,
        contested_quotes={"b": "I multiply by the area"},
    )

    assert calls == []
    assert result.action == "done"


async def test_gate_G8_the_gate_is_inert_below_level_2(monkeypatch):
    """``challenge_gate`` is armed by ``controller._ladder_inputs`` at level >= 2
    and nowhere else, so a level-0/1 deployment cannot serve a challenge even
    with a contested node in hand."""

    async def inline(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(unified.asyncio, "to_thread", inline)
    monkeypatch.setattr(unified, "_call_unified", lambda **kwargs: _draft())

    result = await unified.evaluate_and_ask(
        transcript=[("student", "one"), ("apollo", "why?"), ("student", "I multiply by the area")],
        reference_graph=_graph(),
        problem=SimpleNamespace(problem_text="Why does pressure work?"),
        tally_state=(unified.TallyState("b", "multiply", "tentative", times_asked=1),),
        budget=unified.QuestionBudget(questions_asked=0, cap=8),
        challenge_gate=False,
        contested_quotes={"b": "I multiply by the area"},
    )

    assert result.action == "done"


# --------------------------------------------------------------------------- #
# G9 — the attempt-83 cohort, by playback                                      #
# --------------------------------------------------------------------------- #

_FIXTURES = {fixture.name: fixture for fixture in turn_replay.load_fixtures()}
_COHORT = "attempt_083_paragraph_dump"


async def _matrix(monkeypatch, levels=(0, 1, 2)):
    """Every committed fixture x every level, in PLAYBACK mode (no network)."""
    matrix: dict[tuple[str, int], turn_replay.FixtureReplay] = {}
    for level in levels:
        monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", str(level))
        for name, fixture in sorted(_FIXTURES.items()):
            matrix[(name, level)] = await turn_replay.replay_recorded(fixture)
    return matrix


def test_gate_G9_the_committed_fixture_set_is_the_four_named_regressions():
    """§2.2 names 86/167/124 as mandatory fixtures and §4's G9 row names 83's
    cohort. If the directory ever holds a different set, every gate below is
    measuring something else."""
    assert set(_FIXTURES) == {
        "attempt_083_paragraph_dump",
        "attempt_086_zero_transcript",
        "attempt_124_conflicting_graded",
        "attempt_167_self_correction",
    }


async def test_gate_G9_the_cohort_attempt_is_gated_at_level_2_and_only_there(monkeypatch):
    """Attempt 83: 1 turn, 0 questions asked, every tally row ``times_asked = 0``,
    A+(98) in prod, and Apollo's only reply was the fixed auto-done string. It is
    gate shape (b) — the unprobed single-turn claim — and it fires at level 2,
    the rung the gate ships at, and at no rung below it."""
    matrix = await _matrix(monkeypatch)

    fires = {level: matrix[(_COHORT, level)].turns for level in (0, 1, 2)}
    assert sum(turn.done_gate_fired for turn in fires[0]) == 0
    assert sum(turn.done_gate_fired for turn in fires[1]) == 0
    assert sum(turn.done_gate_fired for turn in fires[2]) == 1


async def test_gate_G9_gating_is_all_this_measures_the_grade_does_not_move(monkeypatch):
    """§4.1, as an executable assertion. The cohort attempt still scores exactly
    what it scored at level 0 — replay cannot generate the answer to a question
    that was never asked, so a moved score here would be an artifact, not a
    result."""
    matrix = await _matrix(monkeypatch)
    grades = {level: matrix[(_COHORT, level)].grade for level in (0, 1, 2)}

    assert grades[0] is not None
    assert {(grade.score, grade.letter) for grade in grades.values()} == {
        (grades[0].score, grades[0].letter)
    }


async def test_gate_G9_every_fixture_keeps_its_level_0_grade_at_levels_1_and_2(monkeypatch):
    """The whole matrix, not just the cohort: levels 1 and 2 move TURN SHAPE and
    never the grade. This is the corpus-level restatement of G-L1/G-L3's
    single-attempt inertness."""
    matrix = await _matrix(monkeypatch)

    for name in _FIXTURES:
        baseline = matrix[(name, 0)].grade
        for level in (1, 2):
            grade = matrix[(name, level)].grade
            if baseline is None:
                assert grade is None, name
                continue
            assert grade is not None
            assert (grade.score, grade.letter) == (baseline.score, baseline.letter), (name, level)


async def test_gate_G9_no_other_fixture_is_gated_at_any_level(monkeypatch):
    """124 and 167 open with an ``ask``, so ``self_declared_done`` is False and
    ``challenge.resolve`` returns ``None`` before anything else; 86 never reaches
    the engine at all. "All gated or individually explained" — this is the
    explanation, pinned."""
    matrix = await _matrix(monkeypatch)

    for (name, level), replay in matrix.items():
        if name == _COHORT:
            continue
        assert sum(turn.done_gate_fired for turn in replay.turns) == 0, (name, level)


async def test_gate_G9_playback_can_never_exercise_the_contradiction_shape(monkeypatch):
    """The honest limitation, as a test rather than a footnote: prod ran
    pre-P3.2, so every reconstructed update is ``wrongness: "none"`` and gate
    shape (a) is unobservable offline. A campaign report that claims a
    contradiction-driven gate fire from PLAYBACK is reading an artifact."""
    matrix = await _matrix(monkeypatch)

    assert all(replay.wrongness_findings == 0 for replay in matrix.values())
