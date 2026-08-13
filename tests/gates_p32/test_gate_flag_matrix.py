"""THE S10 flag matrix: ``APOLLO_WRONGNESS_LEVEL`` 0-4 x concept allowlist.

Every other gate in this suite proves one property at one rung. This module
proves the LADDER: for each level, exactly the behaviours plan §S10 assigns to
that rung activate, and nothing else does. It is the strongest single regression
the gate suite owns — a mis-wired gate site that leaks one rung early fails here
even when every slice-level test still passes, because the slice tests each know
only their own rung.

**The table under test (plan §S10, as amended by wave 2).**

| Behaviour | Active at | Observed here through |
|---|---|---|
| producer ``wrongness`` schema + prompt block | >= 1 | ``_LadderInputs.wrongness`` |
| tagged evidence entry persisted | >= 1 | falls out of the above (no update carries wrongness below 1) |
| adjudicator candidates + ``coverage["wrongness"]`` | >= 1 **and** >= 1 candidate | ``GateRun.wrongness_candidates`` |
| ``apollo_wrongness_observed`` shadow log | >= 1 | the ``done`` logger |
| ``grader_payload -> 'misconceptions'`` shadow record | >= 1 | ``GateRun.shadow_misconceptions`` |
| probe priority (L2a) | >= 2 | ``_LadderInputs.contested_ids`` |
| done-gate (L2b) | >= 2 | ``_LadderInputs.challenge_gate`` |
| carried challenge (L2c) | >= 2 | ``_LadderInputs.carried_challenges`` |
| ``topics[].misconceptions`` / artifact array / scorecard ``watch_out`` | >= 3 | ``GateRun`` |
| XP bonus (+10, D7) | >= 3 | ``GateRun.xp_delta`` |
| ceiling ``min(raw, 84)`` + ``misconception_dock`` | == 4 | ``GateRun.topic_score`` |
| teacher surfaces exclude shadow entries, populate at >= 3 | >= 3 | **xfail — W3-B lands the filter** |

**Amendment recorded (wave-2 §2, and the orchestrator ruling this task was given).**
S10 as written says the persisted array starts at level 3. It does not: L2c reads
what an EARLIER attempt persisted, and the decision-7 XP dedup subtracts the same
rows, so a level-2 cohort reading an array only level-3 attempts ever wrote
would silently no-op at the rung it ships at. The array is therefore written from
level **1**, internal-only — the served payload, the scorecard and
``topics[].misconceptions`` all still start at 3. The ruling adds a ``shadow:
true`` marker below level 3 so teacher surfaces can exclude the internal
population; that marker is W3-B's and is xfail'd here until it lands.

The **allowlist** axis is the INTERACTION5 pairing: an ``INTERACTION_CONCEPTS``
that does not name this problem's concept forces the WHOLE ladder to 0 at every
level, which is how a pilot scopes itself to one concept.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.schemas.problem import Problem
from apollo.smart_questions import controller
from apollo.smart_questions.unified import TallyState
from tests.gates_p32 import k_criteria
from tests.gates_p32._harness import run_gate_done

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit

_LEVELS = (0, 1, 2, 3, 4)
_NODE = "eq1"
_OTHER_CONCEPT = "not_the_pilot_concept"
_DONE_LOGGER = "apollo.handlers.done"


# --------------------------------------------------------------------------- #
# Plane A — the questioning side (`controller._ladder_inputs`)                  #
# --------------------------------------------------------------------------- #


def _problem() -> Problem:
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
                },
                {
                    "step": 2,
                    "entry_type": "procedure_step",
                    "id": "c1",
                    "content": {
                        "action": "state the assumption",
                        "purpose": "bound it",
                        "order": 2,
                    },
                },
            ],
        }
    )


class _CarryRow:
    """A contested ledger row: latest evidence carries a material label."""

    def __init__(self, node_id: str) -> None:
        self.id = 1
        self.reference_node_id = node_id
        self.state = "tentative"
        self.times_asked = 0
        self.last_asked_turn = None
        self.asked_turn = None
        self.answered_turn = None
        self.question = ""
        self.evidence: list[dict[str, Any]] = wf.tagged_evidence()


async def _ladder(monkeypatch, *, level: int, allowlist: str | None):
    """Resolve one turn's ladder inputs at ``level`` under ``allowlist``."""
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", str(level))
    if allowlist is None:
        monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    else:
        monkeypatch.setenv("INTERACTION_CONCEPTS", allowlist)
    # A prior corroborated finding on the OTHER node, so an L2c carry is
    # available whenever the rung permits one.
    monkeypatch.setattr(
        controller,
        "prior_wrongness_findings",
        _async_return(
            (
                {
                    "canonical_key": "c1",
                    "evidence_span": "Something I claimed last attempt.",
                    "resolved": False,
                    "attempt_id": 7,
                },
            )
        ),
    )
    problem = _problem()
    rows = [_CarryRow(_NODE)]
    return await controller._ladder_inputs(
        # Never queried: `prior_wrongness_findings` — the only consumer of the
        # session on this path — is patched above.
        cast("AsyncSession", SimpleNamespace()),
        problem=problem,
        attempt_id=99,
        course_id=3,
        reference_graph=problem.to_kg_graph(attempt_id=99),
        rows=rows,
        tally_state=(
            TallyState(_NODE, "Bernoulli", "tentative", times_asked=0),
            TallyState("c1", "Assumption", "missing", times_asked=0),
        ),
        questions_asked=0,
        cap=8,
    )


def _async_return(value: Any):
    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _call


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_questioning_side_activates_exactly_its_rung(monkeypatch, level):
    ladder = await _ladder(monkeypatch, level=level, allowlist=None)

    assert ladder.wrongness is (level >= 1), "producer schema/prompt block"
    assert ladder.challenge_gate is (level >= 2), "L2b done-gate"
    assert bool(ladder.contested_ids) is (level >= 2), "L2a probe priority"
    assert bool(ladder.contested_quotes) is (level >= 2), "L2b challenge target quote"
    assert bool(ladder.carried_challenges) is (level >= 2), "L2c cross-attempt memory"
    if level >= 2:
        assert ladder.contested_ids == (_NODE,)
        assert len(ladder.carried_challenges) == 1, "decision D4 caps the carry at ONE"


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_a_non_matching_allowlist_forces_the_whole_ladder_off(monkeypatch, level):
    """The INTERACTION5 pairing. A pilot scopes itself by SETTING
    ``INTERACTION_CONCEPTS``; a concept outside it gets the pre-feature build at
    every rung, including 4."""
    ladder = await _ladder(monkeypatch, level=level, allowlist=_OTHER_CONCEPT)

    assert ladder == type(ladder)(), "a non-piloted concept saw a non-default ladder"


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_a_matching_allowlist_is_the_same_as_no_allowlist(monkeypatch, level):
    scoped = await _ladder(monkeypatch, level=level, allowlist=wf.CONCEPT_SLUG)
    ambient = await _ladder(monkeypatch, level=level, allowlist=None)

    assert scoped == ambient


# --------------------------------------------------------------------------- #
# Plane B — the at-Done side (`handle_done`)                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_done_side_activates_exactly_its_rung(monkeypatch, level):
    run = await run_gate_done(monkeypatch, level=level)

    # >= 1 — production, persistence, corroboration
    assert (run.wrongness_candidates is not None) is (level >= 1), "S5 candidate map"
    assert (run.shadow_misconceptions is not None) is (level >= 1), "internal shadow record"

    # >= 3 — the surfaces
    surfaced = level >= 3
    assert bool(run.topic_misconceptions) is surfaced, "topics[].misconceptions"
    assert bool(run.served_misconceptions) is surfaced, "artifact misconceptions"
    assert bool(run.student_response["scorecard"]["watch_out"]) is surfaced, "scorecard watch_out"

    # == 4 — the dark ceiling, and nowhere below it
    assert (run.topic_score["misconception_dock"] > 0) is (level == 4), "misconception_dock"
    assert run.topic_score["score"] == (84 if level == 4 else 100)


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_shadow_log_only_from_level_1(monkeypatch, level, caplog):
    with caplog.at_level(logging.INFO, logger=_DONE_LOGGER):
        await run_gate_done(monkeypatch, level=level)

    summaries = k_criteria.parse_shadow_summaries(caplog.messages)
    assert bool(summaries) is (level >= 1)
    if summaries:
        assert summaries[0].level == level


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_xp_bonus_only_from_level_3(monkeypatch, level):
    """Decision 7, with A's amendment: the bonus population is
    ``resolved AND apollo_elicited``, never ``corroborated`` (which is the empty
    set by construction — S2′ requires NOT ``corrected_later``). The documented
    exception in §S10 is that XP activates at level 3 even though 3 is labelled
    "no score effect": XP is not the grade and only ever goes up."""
    run = await run_gate_done(
        monkeypatch, level=level, wrongness_map=wf.second_reader(corrected_later=True)
    )

    assert run.xp_delta == (20 if level >= 3 else 10)
    assert run.xp_delta >= 10, "the bonus is additive-only; apply_xp raises on a negative delta"


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_a_non_matching_allowlist_forces_done_side_off(monkeypatch, level):
    """The whole Done side, scoped out: identical to a level-0 run at every rung."""
    baseline = await run_gate_done(monkeypatch, level=0)
    scoped_out = await run_gate_done(monkeypatch, level=level, concept_allowlist=_OTHER_CONCEPT)

    assert scoped_out.wrongness_candidates is None
    assert scoped_out.shadow_misconceptions is None
    assert scoped_out.topic_misconceptions == []
    assert scoped_out.served_misconceptions == []
    assert scoped_out.topic_score == baseline.topic_score
    assert scoped_out.xp_delta == baseline.xp_delta


@pytest.mark.parametrize("level", _LEVELS)
async def test_flag_matrix_every_rung_is_a_superset_of_the_one_below(monkeypatch, level):
    """ "The gradient IS the safety design, and the kill switch is a single
    decrement" (§2.3). A rung that turned something OFF relative to the rung
    below would make a decrement unsafe."""
    run = await run_gate_done(monkeypatch, level=level)
    active = {
        "candidates": run.wrongness_candidates is not None,
        "shadow_record": run.shadow_misconceptions is not None,
        "surfaces": bool(run.served_misconceptions),
    }
    expected = {
        "candidates": level >= 1,
        "shadow_record": level >= 1,
        "surfaces": level >= 3,
    }

    assert active == expected


@pytest.mark.xfail(
    reason="W3-B lands the `shadow: true` marker + the teacher-surface filter",
    strict=False,
)
@pytest.mark.parametrize("level", [1, 2, 3])
async def test_flag_matrix_teacher_surfaces_exclude_shadow_entries(monkeypatch, level):
    """ORCHESTRATOR RULING (wave 2/3), not yet in the spec: the persisted
    ``grader_payload -> 'misconceptions'`` array carries ``"shadow": true`` below
    level 3, so ``classroom.top_misconceptions`` — a live SQL LATERAL with no
    level predicate — can exclude the internal population and keep populating at
    >= 3 as S10 says.

    Without the marker the aggregate lights up at level >= 1 and counts resolved
    findings too (wave-2 F-20). Teacher-only and human-gated, but a deviation
    from S10 either way.
    """
    run = await run_gate_done(monkeypatch, level=level)
    entries = run.shadow_misconceptions or []

    assert entries
    assert all(entry.get("shadow") is (level < 3) for entry in entries)
