"""R8 — the tally-state enum exists in FOUR copies and drifts SILENTLY.

`unified.LearnerState` / `unified._VALID_STATES` gate what the model may send,
`controller._VALID_STATES` gates what is read back off the ledger, and
`transcript_coverage._VALID_TALLY_STATES` gates what the at-Done adjudicator is
told. A value present in one and absent from another is not an error anywhere:
both readers coerce the unknown state to ``"missing"``, so a divergence
silently downgrades live tally rows — the P1.3 prior context evaporates and the
node looks untaught. P3.2 adds a second enum (`wrongness`) whose values must
likewise agree between the value object and the JSON schema.

This test is only useful if it FAILS when one copy is edited; that was verified
by temporarily adding a fifth state to `unified._VALID_STATES` (red), then
reverting (green).
"""

from __future__ import annotations

from typing import get_args

import pytest

from apollo.overseer import transcript_coverage
from apollo.smart_questions import controller, unified

pytestmark = pytest.mark.unit


def test_four_copies_of_the_tally_state_enum_agree():
    literal_states = set(get_args(unified.LearnerState))
    assert literal_states == unified._VALID_STATES
    assert literal_states == controller._VALID_STATES
    assert literal_states == set(transcript_coverage._VALID_TALLY_STATES)


def test_the_schema_offers_exactly_those_states():
    item = unified._schema()["schema"]["properties"]["tally_updates"]["items"]
    assert set(item["properties"]["status"]["enum"]) == unified._VALID_STATES


def test_the_wrongness_enum_agrees_with_its_literal_and_its_schema():
    assert set(get_args(unified.Wrongness)) == unified.WRONGNESS_VALUES
    item = unified._schema(wrongness=True)["schema"]["properties"]["tally_updates"]["items"]
    assert set(item["properties"]["wrongness"]["enum"]) == unified.WRONGNESS_VALUES


def test_the_default_wrongness_is_a_member_of_the_enum():
    """`TallyUpdate.wrongness` defaults to ``"none"`` so every pre-P3.2
    construction stays valid — including the ones in other packages' tests."""
    assert unified.TallyUpdate("n1", "missing").wrongness == "none"
    assert unified.TallyUpdate("n1", "missing").wrongness in unified.WRONGNESS_VALUES
    assert unified.TallyUpdate("n1", "missing").contradiction is None
