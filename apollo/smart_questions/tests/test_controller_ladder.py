"""P3.2 — `APOLLO_WRONGNESS_LEVEL` wiring at the questioning controller.

`plan_next_question` is the ONLY place the ladder level is read on the
questioning path, and it turns exactly one rung on at a time:

* level 0 — every argument `evaluate_and_ask` receives is the pre-P3.2 one, and
  no extra query is issued;
* level 1 — the producer only (``wrongness=True``); scheduling stays dark;
* level 2 — probe priority, the done-gate, and the ONE carried challenge.

The level is resolved through `wrongness.effective_wrongness_level`, the single
flag reader, so the concept allowlist applies here exactly as it does at Done.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apollo.schemas.problem import Problem
from apollo.smart_questions import controller
from apollo.smart_questions.unified import UnifiedQuestionResult

_ATTEMPT = 11
_COURSE = 3
_SESSION = 5
_PROBLEM_DB_ID = 42


def _problem(**overrides) -> Problem:
    payload: dict[str, Any] = {
        "id": "p1",
        "database_id": _PROBLEM_DB_ID,
        "concept_id": "ethics",
        "difficulty": "intro",
        "problem_text": "Explain x?",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "procedure_step",
                "id": "step_y",
                "content": {"action": "combine them", "purpose": "reach x", "order": 1},
            },
            {
                "step": 2,
                "entry_type": "definition",
                "id": "def_x",
                "content": {"concept": "x", "meaning": "the private meaning"},
            },
        ],
    }
    payload.update(overrides)
    return Problem.model_validate(payload)


class _Row:
    """A `QuestionOpportunity`-shaped ledger row."""

    def __init__(self, node_id, *, state="tentative", evidence=None, times_asked=0):
        self.id = 1
        self.reference_node_id = node_id
        self.state = state
        self.evidence = evidence if evidence is not None else []
        self.times_asked = times_asked
        self.last_asked_turn = None
        self.asked_turn = None
        self.answered_turn = None
        self.question = ""


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)

    def mappings(self):
        return _Mappings(self._rows)

    def scalar_one(self):
        return 1


class _Savepoint:
    """`AsyncSession.begin_nested()`'s async-CM shape.

    Modelled rather than stubbed away because `prior_wrongness_findings` runs
    its read inside a SAVEPOINT on purpose: swallowing a DB error without one
    leaves the outer transaction failed, and this fake's whole job is to prove
    the turn survives a failing cross-attempt read."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        self._db.savepoints += 1
        return self

    async def __aexit__(self, *_exc):
        return False


class _DB:
    """Fake session: the ledger SELECT, then any raw-SQL reads, in order."""

    def __init__(self, ledger_rows, *later):
        self._queued = [ledger_rows, *later]
        self.calls: list[tuple] = []
        self.savepoints = 0

    def begin_nested(self):
        return _Savepoint(self)

    async def execute(self, statement, params=None):
        self.calls.append((statement, params))
        return _Result(self._queued.pop(0) if self._queued else [])

    def add(self, row):
        pass

    async def flush(self):
        pass


def _tagged_evidence(quote, *, wrongness="contradicts_material"):
    return [
        {
            "turn_id": 0,
            "quote": quote,
            "wrongness": wrongness,
            "contradicts": "the reference says otherwise",
            "kind": "inverted relationship",
        }
    ]


async def _plan(db, monkeypatch, *, problem=None):
    """Run one turn, capturing the kwargs `evaluate_and_ask` was handed."""
    seen: dict[str, Any] = {}

    async def evaluate(**kwargs):
        seen.update(kwargs)
        return UnifiedQuestionResult((), "done", None, None, None)

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=_COURSE,
        attempt_id=_ATTEMPT,
        session_id=_SESSION,
        problem=problem or _problem(),
        transcript=[("student", "I combine them the other way round")],
        turn_index=0,
    )
    return seen


# --------------------------------------------------------------------------- #
# Level 0 — inert                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_level_zero_passes_every_ladder_argument_off(monkeypatch):
    monkeypatch.delenv("APOLLO_WRONGNESS_LEVEL", raising=False)
    db = _DB([_Row("step_y", evidence=_tagged_evidence("I combine them the other way round"))])
    seen = await _plan(db, monkeypatch)
    assert seen["wrongness"] is False
    assert seen["challenge_gate"] is False
    assert seen["contested_ids"] == ()
    assert seen["contested_quotes"] == {}
    assert seen["budget"].carried_challenges == ()


@pytest.mark.asyncio
async def test_level_zero_issues_no_extra_query(monkeypatch):
    """One SELECT, the ledger read that already existed. The cross-attempt read
    is a level-2 cost and must not be paid below it."""
    monkeypatch.delenv("APOLLO_WRONGNESS_LEVEL", raising=False)
    db = _DB([_Row("step_y")])
    await _plan(db, monkeypatch)
    assert len(db.calls) == 1


@pytest.mark.asyncio
async def test_a_non_numeric_level_is_inert(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "yes-please")
    db = _DB([_Row("step_y")])
    seen = await _plan(db, monkeypatch)
    assert (seen["wrongness"], seen["challenge_gate"]) == (False, False)


@pytest.mark.asyncio
async def test_the_concept_allowlist_forces_the_ladder_off(monkeypatch):
    """INTERACTION pairing: level 2 on a concept outside the pilot is level 0."""
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "some-other-concept")
    db = _DB([_Row("step_y", evidence=_tagged_evidence("I combine them the other way round"))])
    seen = await _plan(db, monkeypatch)
    assert (seen["wrongness"], seen["challenge_gate"]) == (False, False)
    assert len(db.calls) == 1


# --------------------------------------------------------------------------- #
# Level 1 — the producer, and nothing else                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_level_one_wires_the_producer_on_and_scheduling_off(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "1")
    db = _DB([_Row("step_y", evidence=_tagged_evidence("I combine them the other way round"))])
    seen = await _plan(db, monkeypatch)
    assert seen["wrongness"] is True
    assert seen["challenge_gate"] is False
    assert seen["contested_ids"] == ()
    assert seen["contested_quotes"] == {}
    assert seen["budget"].carried_challenges == ()


@pytest.mark.asyncio
async def test_carry_skipped_below_level_2(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "1")
    db = _DB(
        [_Row("step_y")],
        [{"canonical_key": "step_y", "evidence_span": "earlier claim", "resolved": "false"}],
    )
    seen = await _plan(db, monkeypatch)
    assert seen["budget"].carried_challenges == ()
    assert len(db.calls) == 1


# --------------------------------------------------------------------------- #
# Level 2 — scheduling                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_level_two_derives_contested_ids_and_quotes_from_the_ledger(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db = _DB(
        [_Row("step_y", evidence=_tagged_evidence("I combine them the other way round"))],
        [],
    )
    seen = await _plan(db, monkeypatch)
    assert seen["wrongness"] is True
    assert seen["challenge_gate"] is True
    assert seen["contested_ids"] == ("step_y",)
    assert seen["contested_quotes"] == {"step_y": "I combine them the other way round"}


@pytest.mark.asyncio
async def test_an_untagged_ledger_produces_no_contested_ids(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db = _DB([_Row("step_y", evidence=[{"turn_id": 0, "quote": "I combine them"}])], [])
    seen = await _plan(db, monkeypatch)
    assert seen["contested_ids"] == ()
    assert seen["contested_quotes"] == {}


@pytest.mark.asyncio
async def test_self_contradiction_gets_priority_but_never_a_gate_quote(monkeypatch):
    """`contradicts_self` is a dialogue artifact, not a material error: it earns
    probe priority and nothing more."""
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db = _DB(
        [
            _Row(
                "step_y",
                evidence=_tagged_evidence("I combine them", wrongness="contradicts_self"),
            )
        ],
        [],
    )
    seen = await _plan(db, monkeypatch)
    assert seen["contested_ids"] == ("step_y",)
    assert seen["contested_quotes"] == {}


@pytest.mark.asyncio
async def test_an_ungraded_node_never_becomes_a_gate_quote(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db = _DB([_Row("def_x", evidence=_tagged_evidence("the private meaning"))], [])
    seen = await _plan(db, monkeypatch)
    assert seen["contested_ids"] == ("def_x",)  # still worth re-probing
    assert seen["contested_quotes"] == {}  # ...but the gate only owes graded nodes


@pytest.mark.asyncio
async def test_level_two_carries_one_prior_finding(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db = _DB(
        [_Row("step_y")],
        [
            {
                "canonical_key": "step_y",
                "evidence_span": "I combined them the other way round",
                "resolved": "false",
                "attempt_id": 7,
            }
        ],
    )
    seen = await _plan(db, monkeypatch)
    carried = seen["budget"].carried_challenges
    assert len(carried) == 1
    assert carried[0].node_id == "step_y"
    assert carried[0].prior_quote == "I combined them the other way round"
    assert len(db.calls) == 2


@pytest.mark.asyncio
async def test_a_problem_with_no_database_id_skips_the_cross_attempt_read(monkeypatch):
    """`prior_wrongness_findings` keys on the durable `app.problems.id`. Without
    one there is nothing safe to query, and a missing memory costs one question
    — never a grade."""
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db = _DB([_Row("step_y")])
    seen = await _plan(db, monkeypatch, problem=_problem(database_id=None))
    assert seen["budget"].carried_challenges == ()
    assert len(db.calls) == 1


@pytest.mark.asyncio
async def test_a_failing_cross_attempt_read_never_breaks_the_turn(monkeypatch):
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")

    class _Exploding(_DB):
        async def execute(self, statement, params=None):
            self.calls.append((statement, params))
            if params is not None:
                raise RuntimeError("relation internal.grading_runs does not exist")
            return _Result(self._queued.pop(0) if self._queued else [])

    db = _Exploding([_Row("step_y")])
    seen = await _plan(db, monkeypatch)
    assert seen["budget"].carried_challenges == ()
    assert seen["challenge_gate"] is True
    # ...and it failed INSIDE a savepoint, which is what makes "never breaks the
    # turn" true rather than merely logged: without one the swallowed error
    # leaves the transaction failed and the very next statement — the tally
    # write this turn is about to do — raises `PendingRollbackError`.
    assert db.savepoints == 1


@pytest.mark.asyncio
async def test_level_four_still_only_turns_on_the_level_two_scheduling(monkeypatch):
    """The rungs are cumulative: nothing above 2 changes the questioning turn."""
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    db_two = _DB([_Row("step_y", evidence=_tagged_evidence("I combine them"))], [])
    two = await _plan(db_two, monkeypatch)
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "4")
    db_four = _DB([_Row("step_y", evidence=_tagged_evidence("I combine them"))], [])
    four = await _plan(db_four, monkeypatch)
    for key in ("wrongness", "challenge_gate", "contested_ids", "contested_quotes"):
        assert two[key] == four[key]
