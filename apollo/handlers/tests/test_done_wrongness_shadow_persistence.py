"""Level-1 SHADOW persistence — the write that makes L2c reachable at level 2.

Spec §2.3 calls level 1 "produce + persist + shadow-log", and L2c (cross-attempt
question memory) is a level-**2** rung that reads what an EARLIER attempt
persisted, through `attempt_history.prior_wrongness_findings` over
``grader_payload -> 'misconceptions'``. Deriving that array only from
``topics[].misconceptions`` — populated at level >= 3 — starves it: at level 2
every prior attempt wrote ``[]``, so `controller._select_carried` has nothing to
carry and the rung silently no-ops at the level it ships at.

So the array is persisted from level 1, into the DATABASE COLUMN only. Three
claims, one per section below:

1. level 1 persists it and the SERVED payload is byte-identical to level 0;
2. end to end — a level-1 Done's array, put through the exact projection the
   S9 SQL applies, produces exactly ONE carried challenge on the next attempt
   at level 2;
3. level 0 persists nothing, and level >= 3 keeps ONE producer (the scored
   result) rather than two arrays that could disagree.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from apollo.handlers import artifact_writer
from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.schemas.problem import Problem
from apollo.smart_questions import controller
from apollo.smart_questions.unified import UnifiedQuestionResult

pytestmark = pytest.mark.unit

_NODE = "eq1"


def _artifact_kwargs(started: dict[str, Any]) -> dict[str, Any]:
    return started["write_artifacts"].await_args.kwargs


# --------------------------------------------------------------------------- #
# 1. Level 1 persists; level 1 serves nothing                                  #
# --------------------------------------------------------------------------- #


async def test_level_1_done_persists_the_corroborated_array(monkeypatch):
    """The array reaches `write_artifacts` as `shadow_misconceptions`, node-keyed
    and carrying exactly the three keys every reader consumes — plus, below level
    3, the `shadow` marker that keeps the TEACHER surfaces on S10's rung 3 while
    the internal record starts at rung 1 (W3-B)."""
    _out, started = await wf.run_done(monkeypatch, level=1, real_artifact=True)

    assert _artifact_kwargs(started)["shadow_misconceptions"] == [
        {
            "canonical_key": _NODE,
            "resolved": False,
            "evidence_span": wf.MATERIAL_QUOTE,
            "shadow": True,
        }
    ]


async def test_level_1_served_payload_is_byte_identical_to_level_0(monkeypatch):
    """The whole point of shadow persistence: it is invisible to the student.
    The served response — scorecard included — must not move at all."""
    level0, started0 = await wf.run_done(monkeypatch, level=0, real_artifact=True)
    level1, started1 = await wf.run_done(monkeypatch, level=1, real_artifact=True)

    assert json.dumps(level1, sort_keys=True, default=str) == json.dumps(
        level0, sort_keys=True, default=str
    )
    # ...and specifically the two arrays the persisted column could have leaked
    # into: the artifact payload the scorecard is templated from, and the
    # student-facing *Watch out* list itself.
    payload0 = await started0["write_artifacts"].side_effect(None, **_artifact_kwargs(started0))
    payload1 = await started1["write_artifacts"].side_effect(None, **_artifact_kwargs(started1))
    assert payload1["misconceptions"] == payload0["misconceptions"] == []
    assert level1["scorecard"]["watch_out"] == level0["scorecard"]["watch_out"] == []


async def test_the_shadow_array_lands_in_grader_payload_not_in_the_payload(monkeypatch):
    """`_artifact_row` is where the divergence is allowed to exist — and the only
    place. A list, because both readers unroll it with `jsonb_array_elements`."""
    shadow = [{"canonical_key": _NODE, "resolved": False, "evidence_span": wf.MATERIAL_QUOTE}]
    payload = {
        "versions": {"grader": "v1", "reference_graph_hash": None},
        "scores": {"composite": 1.0, "node_coverage": 1.0},
        "abstention": {"abstained": None},
        "grader_used": "llm",
        "node_ledger": [],
        "edge_ledger": [],
        "misconceptions": [],
        "clarification_trace": [],
        "grading_latency_ms": 1,
    }
    attempt = SimpleNamespace(id=7, problem_id=42)
    sess = SimpleNamespace(user_id="u", search_space_id=3, concept_id="c")

    row = artifact_writer._artifact_row(
        attempt=attempt, sess=sess, payload=payload, shadow_misconceptions=shadow
    )
    assert row.grader_payload["misconceptions"] == shadow
    assert isinstance(row.grader_payload["misconceptions"], list)
    # The caller's payload is never mutated — the scorecard is rendered from it.
    assert payload["misconceptions"] == []


async def test_absent_shadow_array_stores_the_payloads_own(monkeypatch):
    payload = {
        "versions": {"grader": "v1", "reference_graph_hash": None},
        "scores": {},
        "abstention": {},
        "grader_used": "llm",
        "node_ledger": [],
        "edge_ledger": [],
        "misconceptions": [{"canonical_key": "z", "resolved": True, "evidence_span": None}],
        "clarification_trace": [],
        "grading_latency_ms": 1,
    }
    row = artifact_writer._artifact_row(
        attempt=SimpleNamespace(id=7, problem_id=42),
        sess=SimpleNamespace(user_id="u", search_space_id=3, concept_id="c"),
        payload=payload,
        shadow_misconceptions=None,
    )
    assert row.grader_payload["misconceptions"] == payload["misconceptions"]


# --------------------------------------------------------------------------- #
# 2. End to end — level-1 write -> level-2 carried challenge                   #
# --------------------------------------------------------------------------- #


def _as_sql_projection(persisted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The array as `attempt_history._PRIOR_WRONGNESS_FINDINGS_SQL` returns it.

    The S9 query selects ``misc ->> '<key>'``, and Postgres' ``->>`` renders a
    JSON scalar as TEXT — so ``false`` arrives as the string ``"false"``, which
    is exactly what `prior_wrongness_findings` compares against. Modelling that
    hop here is the point of the test: it is where a shape mismatch between the
    writer and the reader would hide.
    """
    return [
        {
            "canonical_key": entry["canonical_key"],
            "evidence_span": entry["evidence_span"],
            "resolved": json.dumps(entry["resolved"]),
            "attempt_id": 1,
        }
        for entry in persisted
    ]


def _problem() -> Problem:
    """A problem whose graded node id is the node the level-1 Done flagged."""
    return Problem.model_validate(
        {
            "id": "p1",
            "database_id": 42,
            "concept_id": wf.CONCEPT_SLUG,
            "difficulty": "intro",
            "problem_text": "Explain the pressure relation?",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "procedure_step",
                    "id": _NODE,
                    "content": {
                        "action": "apply Bernoulli",
                        "purpose": "relate p and v",
                        "order": 1,
                    },
                }
            ],
        }
    )


class _Row:
    """A `QuestionOpportunity`-shaped ledger row for the NEXT attempt."""

    def __init__(self, node_id: str) -> None:
        self.id = 1
        self.reference_node_id = node_id
        self.state = "missing"
        self.evidence: list[dict[str, Any]] = []
        self.times_asked = 0
        self.last_asked_turn = None
        self.asked_turn = None
        self.answered_turn = None
        self.question = ""


class _Savepoint:
    """`AsyncSession.begin_nested()`'s async-CM shape — the S9 read runs inside
    a SAVEPOINT so a swallowed DB error cannot leave the Done transaction in a
    failed state (`apply_xp` and the fenced grade commit both follow it)."""

    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _DB:
    """Fake session: the ledger SELECT first, then the S9 raw-SQL read."""

    def __init__(self, *results: list[Any]) -> None:
        self._queued = list(results)
        self.statements: list[Any] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.statements.append(statement)
        rows = self._queued.pop(0) if self._queued else []
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            mappings=lambda: SimpleNamespace(all=lambda: rows),
        )

    def add(self, row: Any) -> None:
        pass

    async def flush(self) -> None:
        pass


async def test_level_1_findings_become_one_carried_challenge_at_level_2(monkeypatch):
    """THE seam: what a level-1 attempt persisted is what the next attempt asks
    about — capped at ONE (decision D4), phrased as continuity, never a penalty."""
    _out, started = await wf.run_done(monkeypatch, level=1, real_artifact=True)
    persisted = _artifact_kwargs(started)["shadow_misconceptions"]
    assert persisted, "the level-1 Done must have persisted something to carry"

    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    db = _DB([_Row(_NODE)], _as_sql_projection(persisted))

    seen: dict[str, Any] = {}

    async def evaluate(**kwargs: Any) -> UnifiedQuestionResult:
        seen.update(kwargs)
        return UnifiedQuestionResult((), "done", None, None, None)

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=3,
        attempt_id=99,
        session_id=5,
        problem=_problem(),
        transcript=[("student", "Same claim as last time.")],
        turn_index=0,
    )

    carried = seen["budget"].carried_challenges
    assert len(carried) == 1
    assert carried[0].node_id == _NODE
    assert carried[0].prior_quote == wf.MATERIAL_QUOTE


async def test_a_level_0_history_carries_nothing(monkeypatch):
    """The regression this whole fix exists for: with the array persisted only at
    level >= 3, a level-2 cohort reads an empty history and L2c is a no-op."""
    _out, started = await wf.run_done(monkeypatch, level=0, real_artifact=True)
    assert _artifact_kwargs(started)["shadow_misconceptions"] is None

    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    db = _DB([_Row(_NODE)], [])

    seen: dict[str, Any] = {}

    async def evaluate(**kwargs: Any) -> UnifiedQuestionResult:
        seen.update(kwargs)
        return UnifiedQuestionResult((), "done", None, None, None)

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=3,
        attempt_id=99,
        session_id=5,
        problem=_problem(),
        transcript=[("student", "Same claim as last time.")],
        turn_index=0,
    )
    assert seen["budget"].carried_challenges == ()


# --------------------------------------------------------------------------- #
# 3. The other rungs                                                           #
# --------------------------------------------------------------------------- #


async def test_level_0_persists_no_array(monkeypatch):
    _out, started = await wf.run_done(monkeypatch, level=0, real_artifact=True)
    assert _artifact_kwargs(started)["shadow_misconceptions"] is None


@pytest.mark.parametrize("level", [3, 4])
async def test_the_persisted_array_agrees_with_the_served_one_at_level_3(monkeypatch, level: int):
    """The persisted column is a SUPERSET of the served array, and the two agree
    entry for entry on the corroborated nodes — the shape that lets the internal
    record carry the XP-dedup rows without the student ever seeing them."""
    _out, started = await wf.run_done(monkeypatch, level=level, real_artifact=True)
    kwargs = _artifact_kwargs(started)
    persisted = kwargs["shadow_misconceptions"]
    payload = await started["write_artifacts"].side_effect(None, **kwargs)

    assert payload["misconceptions"] == [
        {"canonical_key": _NODE, "resolved": False, "evidence_span": wf.MATERIAL_QUOTE}
    ]
    assert persisted == payload["misconceptions"]


async def test_level_2_persists_the_same_array_as_level_1(monkeypatch):
    """Level 2 is where the memory is READ, so it must also still be WRITTEN —
    a cohort running at level 2 builds its own history."""
    _out, one = await wf.run_done(monkeypatch, level=1, real_artifact=True)
    _out2, two = await wf.run_done(monkeypatch, level=2, real_artifact=True)
    assert (
        _artifact_kwargs(two)["shadow_misconceptions"]
        == _artifact_kwargs(one)["shadow_misconceptions"]
    )


async def test_nothing_corroborated_persists_an_empty_array_not_none(monkeypatch):
    """An empty ARRAY, never an object and never a missing key: `jsonb_typeof`
    is what stands between a malformed payload and zero rows at read time."""
    _out, started = await wf.run_done(
        monkeypatch, level=1, wrongness_map=wf.second_reader(contradicted=False)
    )
    assert _artifact_kwargs(started)["shadow_misconceptions"] == []


# --------------------------------------------------------------------------- #
# 4. The XP-dedup loop — the record the bonus subtracts must actually exist    #
# --------------------------------------------------------------------------- #


async def test_a_paid_bonus_writes_the_row_that_suppresses_the_next_one(monkeypatch):
    """The decision-7 guard is "once per user x problem x node", enforced by
    subtracting `prior_wrongness_findings`. Corroborated findings can NEVER be
    resolved (S2′ requires NOT corrected_later), so an array of corroborated
    findings alone records nothing about the population the bonus pays — the
    subtraction would always be empty and +10 would be re-earnable on every
    best-grade-wins retry. The paid node is therefore persisted too."""
    _out, started = await wf.run_done(
        monkeypatch,
        level=3,
        wrongness_map=wf.second_reader(corrected_later=True),
        real_artifact=True,
    )
    assert started["apply_xp"].await_args.kwargs["xp_delta"] == 20  # base 10 + bonus 10

    persisted = _artifact_kwargs(started)["shadow_misconceptions"]
    assert persisted == [
        {"canonical_key": _NODE, "resolved": True, "evidence_span": wf.MATERIAL_QUOTE}
    ]
    # ...and that row, read back through the S9 projection, is exactly what the
    # NEXT attempt subtracts. Retry pays base XP only.
    _out2, retry = await wf.run_done(
        monkeypatch,
        level=3,
        wrongness_map=wf.second_reader(corrected_later=True),
        prior_findings=tuple(
            {
                "canonical_key": row["canonical_key"],
                "evidence_span": row["evidence_span"],
                "resolved": json.dumps(row["resolved"]) == "true",
                "attempt_id": 7,
            }
            for row in persisted
        ),
        real_artifact=True,
    )
    assert retry["apply_xp"].await_args.kwargs["xp_delta"] == 10  # base only, bonus suppressed


async def test_a_resolved_node_is_never_carried_forward_as_a_challenge(monkeypatch):
    """The same row must NOT become an L2c challenge — the student already fixed
    that claim. `_select_carried` skips anything `resolved`."""
    _out, started = await wf.run_done(
        monkeypatch,
        level=3,
        wrongness_map=wf.second_reader(corrected_later=True),
        real_artifact=True,
    )
    persisted = _artifact_kwargs(started)["shadow_misconceptions"]
    assert [row["resolved"] for row in persisted] == [True]

    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    db = _DB([_Row(_NODE)], _as_sql_projection(persisted))

    seen: dict[str, Any] = {}

    async def evaluate(**kwargs: Any) -> UnifiedQuestionResult:
        seen.update(kwargs)
        return UnifiedQuestionResult((), "done", None, None, None)

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=3,
        attempt_id=99,
        session_id=5,
        problem=_problem(),
        transcript=[("student", "Same claim as last time.")],
        turn_index=0,
    )
    assert seen["budget"].carried_challenges == ()


# --------------------------------------------------------------------------- #
# 5. G-L1c needs a denominator, not just a count                              #
# --------------------------------------------------------------------------- #


async def test_the_summary_line_carries_the_over_fire_denominator(monkeypatch, caplog):
    """G-L1c gates the dark bake on "`wrongness != none` on **< 10% of ledger
    rows**". `findings=` is the numerator; without the total the ratio cannot be
    computed from the shadow corpus at all — and producing that corpus is the
    only reason level 1 exists. The default ledger is one tagged entry plus one
    clean one, so the honest reading is 1 of 2."""
    with caplog.at_level(logging.INFO, logger="apollo.handlers.done"):
        await wf.run_done(monkeypatch, level=1)

    line = next(m for m in caplog.messages if m.startswith("apollo_wrongness_summary"))
    assert "findings=1" in line
    assert "ledger_entries=2" in line
