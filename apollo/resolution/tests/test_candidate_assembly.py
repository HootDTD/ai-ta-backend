from types import SimpleNamespace

import pytest

from apollo.resolution import candidate_assembly


@pytest.mark.asyncio
async def test_load_problem_candidates_builds_reference_only_inputs(monkeypatch):
    specs = [SimpleNamespace(canonical_key="eq.a", key=7)]
    async def load_specs(*args, **kwargs):
        return specs

    captured = {}

    def build(problem_payload, misconception_payload, *, canon_key_by_canonical_key):
        captured.update(
            problem_payload=problem_payload,
            misconception_payload=misconception_payload,
            canon_keys=canon_key_by_canonical_key,
        )
        return SimpleNamespace(candidates=("candidate",), symbolic_mappings={})

    monkeypatch.setattr(candidate_assembly, "load_entity_specs", load_specs)
    monkeypatch.setattr(candidate_assembly, "build_problem_candidates", build)

    inputs, soundness = await candidate_assembly.load_problem_candidates_with_soundness(
        object(), search_space_id=1, concept_id=2, problem_payload={"id": "p1"}
    )

    assert inputs.candidates == ("candidate",)
    assert soundness is False
    assert captured == {
        "problem_payload": {"id": "p1"},
        "misconception_payload": {"misconceptions": []},
        "canon_keys": {"eq.a": 7},
    }


@pytest.mark.asyncio
async def test_load_problem_candidates_returns_inputs_only(monkeypatch):
    expected = SimpleNamespace(candidates=())

    async def loader(*args, **kwargs):
        return expected, False

    monkeypatch.setattr(candidate_assembly, "load_problem_candidates_with_soundness", loader)

    result = await candidate_assembly.load_problem_candidates(
        object(), search_space_id=1, concept_id=None, problem_payload={}
    )

    assert result is expected
