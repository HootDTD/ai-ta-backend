"""Pure strategy-enumeration tests; every chat call is stubbed."""

from __future__ import annotations

import json

import pytest

from apollo.provisioning.path_enumeration import (
    build_path_enumeration_schema,
    enumerate_strategy_paths,
    multi_path_enabled,
)


def _problem() -> dict:
    return {
        "problem_text": "Solve the example.",
        "reference_solution": [
            {
                "id": "a",
                "entry_type": "equation",
                "entity_key": "eq.a",
                "content": {"symbolic": "x = 1"},
                "depends_on": [],
            },
            {
                "id": "b",
                "entry_type": "equation",
                "entity_key": "eq.b",
                "content": {"symbolic": "y = 1"},
                "depends_on": [],
            },
            {
                "id": "combine",
                "entry_type": "procedure_step",
                "entity_key": "proc.combine",
                "content": {"action": "combine inputs"},
                "depends_on": ["a", "b"],
            },
            {
                "id": "finish",
                "entry_type": "procedure_step",
                "entity_key": "proc.finish",
                "content": {"action": "finish"},
                "depends_on": ["combine"],
            },
        ],
    }


def _chat(paths: list[dict], calls: list[dict]):
    def chat_fn(**kwargs):
        calls.append(kwargs)
        return json.dumps({"paths": paths})

    return chat_fn


def test_flag_defaults_off_and_reads_environment_per_call(monkeypatch):
    monkeypatch.delenv("APOLLO_MULTI_PATH", raising=False)
    assert multi_path_enabled() is False
    monkeypatch.setenv("APOLLO_MULTI_PATH", "true")
    assert multi_path_enabled() is True
    monkeypatch.setenv("APOLLO_MULTI_PATH", "0")
    assert multi_path_enabled() is False


def test_enumeration_makes_one_strict_low_temperature_call():
    calls: list[dict] = []
    result = enumerate_strategy_paths(_problem(), chat_fn=_chat([], calls))
    assert result == []
    assert len(calls) == 1
    assert calls[0]["purpose"] == "path_enumeration"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert build_path_enumeration_schema()["schema"]["additionalProperties"] is False
    messages = calls[0]["messages"]
    assert "JOINTLY use every supplied step" in messages[0]["content"]
    assert "Qualitative and prose solutions almost always require []" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["reference_solution"][-1]["depends_on"] == ["combine"]


@pytest.mark.parametrize(
    "candidates",
    [
        [
            {"strategy_id": "unknown", "nodes": ["a", "ghost"], "milestones": ["ghost"]},
            {"strategy_id": "peer", "nodes": ["b", "finish"], "milestones": ["finish"]},
        ],
        [
            {"strategy_id": "subset", "nodes": ["a", "finish"], "milestones": ["finish"]},
            {
                "strategy_id": "superset",
                "nodes": ["a", "b", "combine", "finish"],
                "milestones": ["finish"],
            },
        ],
        [
            {
                "strategy_id": "duplicate",
                "nodes": ["a", "combine", "finish"],
                "milestones": ["finish"],
            },
            {
                "strategy_id": "duplicate",
                "nodes": ["b", "combine", "finish"],
                "milestones": ["finish"],
            },
        ],
        [
            {"strategy_id": "bad_milestone", "nodes": ["a", "finish"], "milestones": ["a"]},
            {"strategy_id": "peer", "nodes": ["b", "combine", "finish"], "milestones": ["finish"]},
        ],
        [{"bad": "shape"}, {"strategy_id": "peer", "nodes": ["b"], "milestones": ["b"]}],
    ],
)
def test_enumeration_rejects_invalid_candidate_sets(candidates):
    assert enumerate_strategy_paths(_problem(), chat_fn=_chat(candidates, [])) == []


def test_enumeration_propagates_chat_failure_for_promote_to_fail_safe():
    def chat_fn(**_kwargs):
        raise RuntimeError("stubbed outage")

    with pytest.raises(RuntimeError, match="stubbed outage"):
        enumerate_strategy_paths(_problem(), chat_fn=chat_fn)


def test_enumeration_returns_valid_jointly_covering_distinct_paths():
    candidates = [
        {
            "strategy_id": "x_first",
            "nodes": ["a", "combine", "finish"],
            "milestones": ["finish"],
        },
        {
            "strategy_id": "y_first",
            "nodes": ["b", "combine", "finish"],
            "milestones": ["finish"],
        },
    ]
    assert enumerate_strategy_paths(_problem(), chat_fn=_chat(candidates, [])) == candidates


def test_enumeration_rejects_single_route_even_when_valid_by_itself():
    candidate = {
        "strategy_id": "only",
        "nodes": ["a", "b", "combine", "finish"],
        "milestones": ["finish"],
    }
    assert enumerate_strategy_paths(_problem(), chat_fn=_chat([candidate], [])) == []


def test_enumeration_rejects_non_envelope_response():
    def chat_fn(**_kwargs):
        return "[]"

    with pytest.raises(ValueError, match="paths list"):
        enumerate_strategy_paths(_problem(), chat_fn=chat_fn)
