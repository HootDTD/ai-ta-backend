from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apollo.clarification import candidate_assembly


@pytest.mark.asyncio
async def test_candidate_assembly_uses_only_problem_and_canonical_entities(monkeypatch):
    specs = [SimpleNamespace(canonical_key="eq.one", key="canon.eq.one")]
    monkeypatch.setattr(candidate_assembly, "load_entity_specs", AsyncMock(return_value=specs))
    captured = {}

    def fake_build(payload, misconceptions, *, canon_key_by_canonical_key):
        captured.update(
            payload=payload,
            misconceptions=misconceptions,
            mapping=canon_key_by_canonical_key,
        )
        return SimpleNamespace(candidates=())

    monkeypatch.setattr(candidate_assembly, "build_problem_candidates", fake_build)
    inputs, soundness = await candidate_assembly.load_problem_candidates_with_soundness(
        object(), search_space_id=7, concept_id=9, problem_payload={"id": "p1"}
    )

    assert inputs.candidates == ()
    assert soundness is False
    assert captured == {
        "payload": {"id": "p1"},
        "misconceptions": {"misconceptions": []},
        "mapping": {"eq.one": "canon.eq.one"},
    }


@pytest.mark.asyncio
async def test_chat_candidate_delegate_returns_inputs(monkeypatch):
    expected = SimpleNamespace(candidates=())
    loader = AsyncMock(return_value=(expected, False))
    monkeypatch.setattr(candidate_assembly, "load_problem_candidates_with_soundness", loader)
    result = await candidate_assembly.load_problem_candidates(
        object(), search_space_id=1, concept_id=None, problem_payload={}
    )
    assert result is expected
