"""Unit tests for the S1-S5 stage-audit judges (Plan Task E1).

Everything runs against a :class:`FakeLLM` — no network. What's under test:
input-assembly (each judge builds exactly the right items from its stage's
run-dir-shaped data), prompt assembly (the LLM only ever sees its own
stage's fields), verdict parsing, and the deterministic aggregation math
(``pass_rate = ok / total``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from campaign.judges.base import (
    OpenAIJudgeClient,
    StageJudge,
    Verdict,
    aggregate,
    load_jsonl,
    verdict_schema,
)
from campaign.judges.s1_reference_graph import S1ReferenceGraphJudge, find_structural_defects
from campaign.judges.s2_ingestion import S2IngestionJudge, check_verify_path_fired
from campaign.judges.s3_student_fidelity import S3StudentFidelityJudge, ledger_vs_expected
from campaign.judges.s4_apollo_coherence import S4ApolloCoherenceJudge
from campaign.judges.s5_misconceptions import S5MisconceptionJudge, misconception_recall


class FakeLLM:
    """Records every call it receives and returns canned verdicts by index or
    by a per-item override keyed on ``item_id`` substring."""

    def __init__(
        self, *, default_ok: bool = True, overrides: dict[str, dict[str, Any]] | None = None
    ):
        self.calls: list[dict[str, Any]] = []
        self._default_ok = default_ok
        self._overrides = overrides or {}

    async def judge_item(
        self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt, "schema": schema}
        )
        for key, response in self._overrides.items():
            if key in user_prompt:
                return response
        return {"ok": self._default_ok, "reason": "fake"}


def run(coro):
    return asyncio.run(coro)


# --- base.py -----------------------------------------------------------------


def test_aggregate_empty_is_zero_not_hundred_percent():
    passed, total, pass_rate = aggregate([])
    assert (passed, total, pass_rate) == (0, 0, 0.0)


def test_aggregate_pass_rate_math():
    verdicts = [Verdict("a", True, ""), Verdict("b", True, ""), Verdict("c", False, "bad")]
    passed, total, pass_rate = aggregate(verdicts)
    assert passed == 2
    assert total == 3
    assert pass_rate == pytest.approx(2 / 3)


def test_judge_result_failures_property():
    from campaign.judges.base import JudgeResult

    verdicts = (Verdict("a", True, ""), Verdict("b", False, "bad"))
    result = JudgeResult(stage="s1", verdicts=verdicts, passed=1, total=2, pass_rate=0.5)
    assert result.failures == (Verdict("b", False, "bad"),)


def test_verdict_schema_is_strict_and_closed():
    schema = verdict_schema("s1_reference_graph_verdict")
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == {"ok", "reason"}
    # Fresh dict per call (no shared mutable state across judges).
    assert verdict_schema("x") is not verdict_schema("x")


def test_load_jsonl_missing_file_is_empty(tmp_path: Path):
    assert load_jsonl(tmp_path / "nope.jsonl") == []


def test_load_jsonl_round_trip(tmp_path: Path):
    path = tmp_path / "items.jsonl"
    path.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
    assert load_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_stage_judge_base_build_items_not_implemented():
    judge = StageJudge(llm=FakeLLM())
    with pytest.raises(NotImplementedError):
        judge.build_items({})


def test_openai_judge_client_offloads_to_thread(monkeypatch):
    client = OpenAIJudgeClient(model="gpt-4o-test")
    monkeypatch.setattr(client, "_call", lambda **kwargs: {"ok": True, "reason": "stubbed"})
    result = run(client.judge_item(system_prompt="s", user_prompt="u", schema=verdict_schema("x")))
    assert result == {"ok": True, "reason": "stubbed"}


def test_openai_judge_client_default_model_env(monkeypatch):
    monkeypatch.delenv("APOLLO_JUDGE_MODEL", raising=False)
    assert OpenAIJudgeClient()._model == "gpt-4o"
    monkeypatch.setenv("APOLLO_JUDGE_MODEL", "gpt-4o-mini")
    assert OpenAIJudgeClient()._model == "gpt-4o-mini"


# --- S1 reference graph --------------------------------------------------


def _s1_subject():
    return {
        "subject": "bernoulli_principle",
        "problem": {"statement": "A fluid flows through a pipe..."},
        "nodes": [
            {"node_id": "eq1", "node_type": "equation"},
            {"node_id": "cond1", "node_type": "condition"},
        ],
        "edges": [
            {"edge_type": "PRECEDES", "from_node_id": "cond1", "to_node_id": "eq1"},
        ],
    }


def test_s1_build_items_one_per_node_and_edge_plus_no_structural_defects():
    judge = S1ReferenceGraphJudge(llm=FakeLLM())
    items = judge.build_items([_s1_subject()])
    kinds = [item["kind"] for item in items]
    assert kinds.count("node") == 2
    assert kinds.count("edge") == 1
    assert kinds.count("structural") == 0


def test_s1_structural_defect_duplicate_node_id():
    nodes = [{"node_id": "eq1"}, {"node_id": "eq1"}]
    defects = find_structural_defects(nodes, [])
    assert len(defects) == 1
    assert not defects[0].ok
    assert "duplicate" in defects[0].item_id


def test_s1_structural_defect_precedes_cycle():
    nodes = [{"node_id": "a"}, {"node_id": "b"}]
    edges = [
        {"edge_type": "PRECEDES", "from_node_id": "a", "to_node_id": "b"},
        {"edge_type": "PRECEDES", "from_node_id": "b", "to_node_id": "a"},
    ]
    defects = find_structural_defects(nodes, edges)
    assert any(d.item_id == "structure:cycle" for d in defects)


def test_s1_no_structural_defects_on_a_dag():
    nodes = [{"node_id": "a"}, {"node_id": "b"}, {"node_id": "c"}]
    edges = [
        {"edge_type": "PRECEDES", "from_node_id": "a", "to_node_id": "b"},
        {"edge_type": "PRECEDES", "from_node_id": "b", "to_node_id": "c"},
    ]
    assert find_structural_defects(nodes, edges) == []


def test_s1_structural_defects_ignore_edges_referencing_unknown_nodes():
    nodes = [{"node_id": "a"}, {"node_id": "b"}]
    edges = [
        {"edge_type": "PRECEDES", "from_node_id": "a", "to_node_id": "ghost"},
        {"edge_type": "PRECEDES", "from_node_id": "a", "to_node_id": "b"},
    ]
    assert find_structural_defects(nodes, edges) == []


def test_s1_judge_includes_structural_verdict_without_llm_call():
    subject = _s1_subject()
    subject["nodes"].append({"node_id": "eq1"})  # duplicate -> structural defect
    llm = FakeLLM(default_ok=True)
    judge = S1ReferenceGraphJudge(llm=llm)
    result = run(judge.judge([subject]))
    structural = [v for v in result.verdicts if v.item_id.endswith(":structure:duplicate:eq1")]
    assert len(structural) == 1
    assert not structural[0].ok
    # LLM was called for the real nodes/edges but never asked to judge the
    # structural defect itself (that's pure code).
    for call in llm.calls:
        assert "duplicate" not in call["user_prompt"]


def test_s1_pass_rate_reflects_llm_and_structural_failures():
    subject = _s1_subject()
    llm = FakeLLM(default_ok=False)
    judge = S1ReferenceGraphJudge(llm=llm)
    result = run(judge.judge([subject]))
    assert result.total == 3  # 2 nodes + 1 edge, no structural defects
    assert result.passed == 0
    assert result.pass_rate == 0.0


def test_s1_user_prompt_only_carries_subject_scoped_fields():
    judge = S1ReferenceGraphJudge(llm=FakeLLM())
    items = judge.build_items([_s1_subject()])
    node_item = next(i for i in items if i["kind"] == "node")
    prompt = json.loads(judge.user_prompt(node_item))
    assert set(prompt.keys()) == {"kind", "problem", "entity"}


# --- S2 ingestion ----------------------------------------------------------


def _s2_item(**overrides):
    base = {
        "item_id": "p3",
        "page_ref": "page-3.png",
        "scraped_label": "Problem 3",
        "paired_solution": {"answer": "42"},
        "ocr_confidence": 0.95,
        "low_confidence_threshold": 0.7,
        "verify_path_fired": False,
    }
    base.update(overrides)
    return base


def test_s2_build_items_passthrough():
    judge = S2IngestionJudge(llm=FakeLLM())
    items = judge.build_items([_s2_item()])
    assert items == [_s2_item()]


def test_s2_verify_path_fired_correctly_when_confident():
    verdicts = check_verify_path_fired([_s2_item(ocr_confidence=0.95, verify_path_fired=False)])
    assert len(verdicts) == 1
    assert verdicts[0].ok


def test_s2_verify_path_should_have_fired_but_did_not():
    verdicts = check_verify_path_fired(
        [_s2_item(ocr_confidence=0.5, low_confidence_threshold=0.7, verify_path_fired=False)]
    )
    assert not verdicts[0].ok
    assert "should_fire=True" in verdicts[0].reason


def test_s2_verify_path_fired_when_it_should_not_have():
    verdicts = check_verify_path_fired(
        [_s2_item(ocr_confidence=0.95, low_confidence_threshold=0.7, verify_path_fired=True)]
    )
    assert not verdicts[0].ok


def test_s2_verify_path_skips_items_without_confidence_fields():
    verdicts = check_verify_path_fired([{"item_id": "no-conf"}])
    assert verdicts == []


def test_s2_judge_folds_llm_and_verify_path_verdicts_into_one_pass_rate():
    llm = FakeLLM(default_ok=True)
    judge = S2IngestionJudge(llm=llm)
    result = run(judge.judge([_s2_item()]))
    assert result.total == 2  # 1 LLM verdict + 1 verify-path verdict
    assert result.passed == 2
    assert len(llm.calls) == 1


def test_s2_user_prompt_excludes_confidence_fields():
    judge = S2IngestionJudge(llm=FakeLLM())
    prompt = json.loads(judge.user_prompt(_s2_item()))
    assert set(prompt.keys()) == {"page_ref", "scraped_label", "paired_solution"}


# --- S3 student fidelity ----------------------------------------------------


def _s3_attempt():
    return {
        "attempt_id": "a1",
        "transcript": "Bernoulli's equation relates pressure and velocity...",
        "node_ledger": [
            {
                "key": "eq.bernoulli",
                "status": "credited",
                "evidence_span": "Bernoulli's equation...",
            },
            {"key": "cond.incompressible", "status": "unresolved"},
            {
                "key": "misc.pressure_confusion",
                "status": "misconception",
                "evidence_span": "pressure decreases...",
            },
            {"key": "meta.ignored", "status": "not_a_real_status"},
        ],
        "expected": {
            "credited": ["eq.bernoulli"],
            "unresolved": ["cond.incompressible"],
            "misconceptions": ["misc.pressure_confusion"],
        },
    }


def test_s3_build_items_skips_unaudited_statuses():
    judge = S3StudentFidelityJudge(llm=FakeLLM())
    items = judge.build_items([_s3_attempt()])
    assert len(items) == 3
    assert {i["key"] for i in items} == {
        "eq.bernoulli",
        "cond.incompressible",
        "misc.pressure_confusion",
    }


def test_s3_item_id_scoped_to_attempt():
    judge = S3StudentFidelityJudge(llm=FakeLLM())
    items = judge.build_items([_s3_attempt()])
    assert all(item["item_id"].startswith("a1:") for item in items)


def test_s3_ledger_vs_expected_perfect_agreement():
    attempt = _s3_attempt()
    diff = ledger_vs_expected(attempt["node_ledger"], attempt["expected"])
    assert diff["credited"]["agreement"] == 1.0
    assert diff["unresolved"]["agreement"] == 1.0
    assert diff["misconceptions"]["agreement"] == 1.0
    assert diff["credited"]["missing"] == []
    assert diff["credited"]["unexpected"] == []


def test_s3_ledger_vs_expected_reports_missing_and_unexpected():
    ledger = [
        {"key": "eq.bernoulli", "status": "unresolved"},  # expected credited -> missed
        {"key": "extra.thing", "status": "credited"},  # not expected -> unexpected credit
    ]
    expected = {"credited": ["eq.bernoulli"], "unresolved": [], "misconceptions": []}
    diff = ledger_vs_expected(ledger, expected)
    assert diff["credited"]["missing"] == ["eq.bernoulli"]
    assert diff["credited"]["unexpected"] == ["extra.thing"]
    assert diff["credited"]["agreement"] == 0.0


def test_s3_ledger_vs_expected_empty_expected_set_is_full_agreement():
    diff = ledger_vs_expected([], {"credited": [], "unresolved": [], "misconceptions": []})
    assert diff["credited"]["agreement"] == 1.0


def test_s3_judge_exposes_ledger_vs_expected_in_extra_not_gated():
    judge = S3StudentFidelityJudge(llm=FakeLLM(default_ok=True))
    result = run(judge.judge([_s3_attempt()]))
    assert result.total == 3
    assert "ledger_vs_expected" in result.extra
    assert "a1" in result.extra["ledger_vs_expected"]


def test_s3_judge_only_sees_this_attempts_transcript_and_ledger_entry():
    judge = S3StudentFidelityJudge(llm=FakeLLM())
    items = judge.build_items([_s3_attempt()])
    credited_item = next(i for i in items if i["key"] == "eq.bernoulli")
    prompt = json.loads(judge.user_prompt(credited_item))
    assert set(prompt.keys()) == {"key", "status", "evidence_span", "transcript"}
    assert prompt["transcript"] == _s3_attempt()["transcript"]


def test_s3_pass_rate_flags_phantom_credit():
    llm = FakeLLM(overrides={"eq.bernoulli": {"ok": False, "reason": "not actually taught there"}})
    judge = S3StudentFidelityJudge(llm=llm)
    result = run(judge.judge([_s3_attempt()]))
    phantom = [v for v in result.verdicts if v.item_id == "a1:eq.bernoulli"]
    assert len(phantom) == 1
    assert not phantom[0].ok


# --- S4 Apollo coherence ----------------------------------------------------


def _s4_session():
    return {
        "attempt_id": "a2",
        "apollo_questions": ["Why is the fluid incompressible here?"],
        "clarification_trace": [{"question": "incompressible?", "answer": "yes", "credited": True}],
        "unresolved_keys": [],
        "misconception_keys": [],
    }


def test_s4_build_items_one_per_session():
    judge = S4ApolloCoherenceJudge(llm=FakeLLM())
    items = judge.build_items([_s4_session(), _s4_session()])
    assert len(items) == 2
    assert items[0]["item_id"] == "a2"


def test_s4_user_prompt_shape():
    judge = S4ApolloCoherenceJudge(llm=FakeLLM())
    items = judge.build_items([_s4_session()])
    prompt = json.loads(judge.user_prompt(items[0]))
    assert set(prompt.keys()) == {
        "apollo_questions",
        "clarification_trace",
        "unresolved_keys",
        "misconception_keys",
    }


def test_s4_gate_is_session_level_not_utterance_level():
    llm = FakeLLM(default_ok=True)
    judge = S4ApolloCoherenceJudge(llm=llm)
    result = run(judge.judge([_s4_session(), _s4_session(), _s4_session()]))
    assert result.total == 3  # one verdict per SESSION, not per question/exchange
    assert len(llm.calls) == 3


# --- S5 misconceptions -------------------------------------------------------


def _s5_attempt():
    return {
        "attempt_id": "a3",
        "expected": {"misconceptions": ["misc.pressure_confusion", "misc.unfound"]},
        "asserted_misconceptions": [
            {
                "key": "misc.pressure_confusion",
                "utterance": "pressure always increases with speed",
                "bank_description": "student believes pressure rises with velocity (inverted Bernoulli)",
            }
        ],
    }


def test_s5_build_items_one_per_asserted_misconception():
    judge = S5MisconceptionJudge(llm=FakeLLM())
    items = judge.build_items([_s5_attempt()])
    assert len(items) == 1
    assert items[0]["item_id"] == "a3:misc.pressure_confusion"


def test_s5_user_prompt_excludes_expected_and_key():
    judge = S5MisconceptionJudge(llm=FakeLLM())
    items = judge.build_items([_s5_attempt()])
    prompt = json.loads(judge.user_prompt(items[0]))
    assert set(prompt.keys()) == {"utterance", "bank_description"}


def test_s5_precision_gate_math():
    llm = FakeLLM(default_ok=True)
    judge = S5MisconceptionJudge(llm=llm)
    result = run(judge.judge([_s5_attempt()]))
    assert result.total == 1
    assert result.pass_rate == 1.0


def test_s5_recall_reported_not_gated():
    llm = FakeLLM(default_ok=True)
    judge = S5MisconceptionJudge(llm=llm)
    result = run(judge.judge([_s5_attempt()]))
    recall = result.extra["recall"]
    assert recall["overall_recall"] == pytest.approx(0.5)  # found 1 of 2 expected
    assert recall["per_attempt"]["a3"]["missed"] == ["misc.unfound"]


def test_misconception_recall_no_expected_is_perfect_recall():
    recall = misconception_recall(
        [{"attempt_id": "x", "expected": {}, "asserted_misconceptions": []}]
    )
    assert recall["overall_recall"] == 1.0


def test_misconception_recall_no_attempts_is_perfect_recall():
    assert misconception_recall([])["overall_recall"] == 1.0
