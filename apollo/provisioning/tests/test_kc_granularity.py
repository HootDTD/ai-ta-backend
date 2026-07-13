"""Flag selection for knowledge-component-grained graph derivation."""

import hashlib
import json

import pytest

from apollo.provisioning.authored_sets.graph_derivation import (
    _DERIVATION_SYSTEM_PROMPT_LEGACY,
    derive_reference_graph,
    find_derivation_defects,
    kc_granularity_enabled,
)
from apollo.provisioning.solution import GroundingSpan

_LEGACY_PROMPT_SHA256 = "42f946a091dff133dc7d592e59461984f15bafeab5a8f0985e73d98d45a2359c"
_VOCAB = {"symbols": [], "description": {}, "subscript_convention": ""}
_SPANS = (GroundingSpan(text="A worked explanation.", carries_solution=True),)


class _Candidate:
    problem_text = "Explain the idea."
    given_values: dict = {}
    target_unknown = "idea"
    difficulty = "intro"
    chunk_content_hash = "kc-granularity-test"


def _step(step: int, *, depends_on: list[str] | None = None) -> dict:
    step_id = f"explanation_part_{step}"
    return {
        "step": step,
        "entry_type": "procedure_step",
        "id": step_id,
        "content": {
            "label": f"Explanation part {step}",
            "order": step,
            "action": f"Explain separately assessable idea {step}",
            "purpose": "Build the complete explanation",
            "uses_equations": [],
        },
        "depends_on": depends_on or [],
    }


def _payload(node_count: int = 5) -> dict:
    steps = [
        _step(i, depends_on=[] if i == 1 else [f"explanation_part_{i - 1}"])
        for i in range(1, node_count + 1)
    ]
    return {
        "reference_solution": steps,
        "target_unknown": "idea",
        "symbolic_mappings": {},
        "bound_variables": [],
    }


def _chat(payload: dict):
    calls: list[dict] = []

    def chat_fn(**kwargs) -> str:
        calls.append(kwargs)
        return json.dumps(payload)

    chat_fn.calls = calls  # type: ignore[attr-defined]
    return chat_fn


async def _captured_prompt() -> str:
    chat_fn = _chat(_payload())
    await derive_reference_graph(
        _Candidate(),
        _SPANS,
        concept_slug="qualitative_idea",
        concept_display_name="Qualitative Idea",
        canonical_symbols=_VOCAB,
        normalization_map={},
        chat_fn=chat_fn,
    )
    return chat_fn.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_flag_off_uses_byte_identical_legacy_prompt(monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_KC_GRANULARITY", "0")

    prompt = await _captured_prompt()

    assert prompt == _DERIVATION_SYSTEM_PROMPT_LEGACY
    assert hashlib.sha256(prompt.encode()).hexdigest() == _LEGACY_PROMPT_SHA256


@pytest.mark.asyncio
async def test_flag_on_uses_kc_prompt(monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_KC_GRANULARITY", "TrUe")

    prompt = await _captured_prompt()

    assert "3 to 15" in prompt
    assert "one node per KNOWLEDGE COMPONENT" in prompt
    assert "separately assessable" in prompt
    assert "5 to 9" not in prompt
    assert "5-9 nodes total" not in prompt


@pytest.mark.asyncio
async def test_flag_defaults_off_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("APOLLO_KC_GRANULARITY", raising=False)

    assert not kc_granularity_enabled()
    assert await _captured_prompt() == _DERIVATION_SYSTEM_PROMPT_LEGACY


def test_flag_selects_wide_validator_bound(monkeypatch) -> None:
    graph = {
        "id": "kc.bound",
        "concept_id": "qualitative_idea",
        "difficulty": "intro",
        "problem_text": _Candidate.problem_text,
        "given_values": {},
        **_payload(node_count=3),
    }

    monkeypatch.delenv("APOLLO_KC_GRANULARITY", raising=False)
    legacy_defects = find_derivation_defects(graph, canonical_symbols=_VOCAB, normalization_map={})
    monkeypatch.setenv("APOLLO_KC_GRANULARITY", "yes")
    kc_defects = find_derivation_defects(graph, canonical_symbols=_VOCAB, normalization_map={})

    assert any(defect.startswith("node_count") for defect in legacy_defects)
    assert not any(defect.startswith("node_count") for defect in kc_defects)
