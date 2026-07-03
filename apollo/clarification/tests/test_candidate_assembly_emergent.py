"""Emergent-store read-path wiring in candidate_assembly (memo increment 1).

Proves flag-OFF dormancy (byte-identical candidate set + bank_applicable
regardless of promoted-store contents) and flag-ON exposure (promoted emergent
misconceptions become misc.* candidates exactly like bank entries, with
hand-authored keys winning collisions).
"""

from __future__ import annotations

import pytest

from apollo.clarification.candidate_assembly import load_problem_candidates_with_soundness


class _Spec:
    def __init__(self, ck, k):
        self.canonical_key, self.key = ck, k


class _Entry:
    code = "misc.density_ignored"
    trigger_phrases = ["density doesn't matter"]
    description = "Student ignored density"


_PROBLEM = {
    "reference_solution": [
        {
            "entry_type": "condition",
            "entity_key": "cond.bernoulli",
            "content": {"applies_when": "flow is faster", "aliases": []},
        },
    ]
}

_EMERGENT_PROMOTED = {
    "misconceptions": [
        {
            "key": "misc.emergent_sign",
            "trigger_phrases": ["flipped the sign"],
            "opposes": "eq.newton2",
            "display_name": "misc.emergent_sign",
        }
    ]
}


def _patch(monkeypatch, *, entries, promoted):
    async def fake_load_for_concept(db, *, concept_id):
        return entries

    async def fake_load_entity_specs(db, *, search_space_id, concept_id):
        return [_Spec("cond.bernoulli", 7)]

    async def fake_load_promoted(db, *, search_space_id, concept_id):
        return promoted

    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_for_concept", fake_load_for_concept
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_entity_specs", fake_load_entity_specs
    )
    monkeypatch.setattr(
        "apollo.clarification.candidate_assembly.load_promoted_misconceptions_dict",
        fake_load_promoted,
    )


@pytest.mark.asyncio
async def test_flag_off_ignores_promoted_store(monkeypatch):
    """Flag OFF: even with promoted emergent entries available, the candidate
    set + bank_applicable are byte-identical to the hand-authored-only behavior."""
    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    keys = {c.canonical_key for c in inputs.candidates}
    assert keys == {"cond.bernoulli"}  # emergent key NOT present
    assert bank_applicable is False  # empty hand-authored bank, flag off


@pytest.mark.asyncio
async def test_flag_on_exposes_promoted_as_candidates(monkeypatch):
    """Flag ON + empty hand-authored bank: the promoted emergent misconception
    becomes a misc.* candidate and flips bank_applicable True."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    _patch(monkeypatch, entries=[], promoted=_EMERGENT_PROMOTED)
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    misc = {c.canonical_key for c in inputs.candidates if c.is_misconception}
    assert misc == {"misc.emergent_sign"}
    assert bank_applicable is True


@pytest.mark.asyncio
async def test_flag_on_hand_authored_wins_collision(monkeypatch):
    """Flag ON: a hand-authored key present in BOTH banks appears once, from the
    hand-authored entry (the emergent duplicate is dropped)."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    collision = {
        "misconceptions": [
            {
                "key": "misc.density_ignored",
                "trigger_phrases": ["x"],
                "opposes": "eq.z",
                "display_name": "emergent dup",
            }
        ]
    }
    _patch(monkeypatch, entries=[_Entry()], promoted=collision)
    inputs, _ = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    dupes = [c for c in inputs.candidates if c.canonical_key == "misc.density_ignored"]
    assert len(dupes) == 1
    assert dupes[0].display_name == "Student ignored density"  # hand-authored won


@pytest.mark.asyncio
async def test_flag_on_empty_store_is_cold_start(monkeypatch):
    """Flag ON but no promoted entries (cold-start class): identical to flag-off
    behavior — nothing asserted, bank stays inapplicable on an empty bank."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    _patch(monkeypatch, entries=[], promoted={"misconceptions": []})
    inputs, bank_applicable = await load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload=_PROBLEM
    )
    assert {c.canonical_key for c in inputs.candidates} == {"cond.bernoulli"}
    assert bank_applicable is False
