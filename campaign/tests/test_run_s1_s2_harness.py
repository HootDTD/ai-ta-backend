"""Regression tests for the S1 raw-graph harness scripts.

Two things are pinned here, deliberately in tension:

1. The CANONICAL driver (``campaign/scripts/run_s1_s2.py``) must emit
   ``DEPENDS_ON`` for every ``apollo_entity_prereqs`` row -- per
   ``apollo/ontology/edges.py``, PRECEDES is legal ONLY between
   ``(procedure_step, procedure_step)`` pairs; generic concept->concept
   prerequisite links must be DEPENDS_ON (see
   ``docs/_archive/experiments/2026-07-03-s1-judge-adjudication.md`` sec 2A:
   this mislabel drove 26 of 57 S1 failures in the f1/f1c campaign runs).
   Land all future S1/S2 harness changes in this canonical script.

2. The FROZEN per-run scripts (``campaign/out/f1/run_s1_s2.py`` and
   ``campaign/out/f1c/run_s1_s2.py``) must stay byte-faithful to the
   ``PRECEDES``-labeled edges their committed ``s1-results.json`` was
   recorded against -- they are historical run artifacts, not live code, and
   must NEVER be edited to match the canonical driver's corrected behavior
   (cross-review finding #4). This test pins them to PRECEDES so a future
   edit that silently "fixes" them forward is caught immediately.

Loads each script as a module (they are plain files, not packages) and
drives ``_fetch_subject_graph`` against a fake asyncpg connection -- no real
Postgres needed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from campaign.judges.base import JudgeResult, Verdict
from campaign.scripts import run_s1_s2 as canonical_script

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(rel_path: str, module_name: str) -> ModuleType:
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    """Stands in for an asyncpg.Connection: routes .fetch() by table name
    referenced in the SQL text, returns plain dicts (asyncpg Records support
    the same __getitem__ access the harness uses)."""

    def __init__(self, *, nodes: list[dict[str, Any]], prereqs: list[dict[str, Any]]):
        self._nodes = nodes
        self._prereqs = prereqs

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "apollo_entity_prereqs" in query:
            return self._prereqs
        if "apollo_kg_entities" in query:
            return self._nodes
        if "apollo_concept_problems" in query:
            return []
        raise AssertionError(f"unexpected query: {query!r}")


def _run(coro):
    return asyncio.run(coro)


def _fixture():
    nodes = [
        {
            "id": 1,
            "canonical_key": "fluid_density",
            "kind": "concept",
            "display_name": "Fluid density",
            "payload": json.dumps({}),
        },
        {
            "id": 2,
            "canonical_key": "kinetic_energy_density",
            "kind": "concept",
            "display_name": "Kinetic energy density",
            "payload": json.dumps({}),
        },
    ]
    # from=dependent, to=prerequisite (kinetic_energy_density requires fluid_density)
    prereqs = [{"from_entity_id": 2, "to_entity_id": 1}]
    return nodes, prereqs


# --- canonical driver: must emit DEPENDS_ON -------------------------------


def test_canonical_driver_prereq_rows_emit_depends_on_not_precedes():
    module = _load_module("campaign/scripts/run_s1_s2.py", "_test_canonical_run_s1_s2")
    nodes, prereqs = _fixture()
    conn = _FakeConn(nodes=nodes, prereqs=prereqs)

    graph = _run(module._fetch_subject_graph(conn, "fluid_mechanics", [1]))

    assert graph["edges"] == [
        {
            "edge_type": "DEPENDS_ON",
            "from_node_id": "kinetic_energy_density",
            "to_node_id": "fluid_density",
        }
    ]
    assert "PRECEDES" not in {e["edge_type"] for e in graph["edges"]}


def test_canonical_driver_parse_subjects():
    module = _load_module("campaign/scripts/run_s1_s2.py", "_test_canonical_run_s1_s2_parse")
    assert module.parse_subjects(["fluid_mechanics:1", "macroeconomics:2,3"]) == {
        "fluid_mechanics": [1],
        "macroeconomics": [2, 3],
    }


def test_canonical_driver_build_s2_raw_skips_when_no_fixtures(tmp_path: Path):
    module = _load_module("campaign/scripts/run_s1_s2.py", "_test_canonical_run_s1_s2_s2raw")
    assert module.build_s2_raw(tmp_path) == []


def test_canonical_driver_fetch_subject_graph_parses_problem_payloads():
    # Covers the apollo_concept_problems loop (lines otherwise unexercised by
    # the edge-emission-focused fixture above): both the JSON-string and
    # already-decoded-dict payload shapes.
    nodes, prereqs = _fixture()

    class _ConnWithProblems(_FakeConn):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            if "apollo_concept_problems" in query:
                return [
                    {"problem_code": "p1", "payload": json.dumps({"statement": "A flows..."})},
                    {"problem_code": "p2", "payload": {"statement": "already decoded"}},
                ]
            return await super().fetch(query, *args)

    conn = _ConnWithProblems(nodes=nodes, prereqs=prereqs)
    graph = _run(canonical_script._fetch_subject_graph(conn, "fluid_mechanics", [1]))
    assert graph["problem"]["problems"] == [
        {"statement": "A flows..."},
        {"statement": "already decoded"},
    ]


class _FakeAsyncpgConn(_FakeConn):
    closed = False

    async def close(self) -> None:
        self.closed = True


def test_build_s1_raw_connects_fetches_and_closes(monkeypatch: pytest.MonkeyPatch):
    nodes, prereqs = _fixture()
    conn = _FakeAsyncpgConn(nodes=nodes, prereqs=prereqs)

    async def fake_connect(dsn: str):
        assert dsn == "postgresql://fake-dsn"
        return conn

    monkeypatch.setattr(canonical_script.asyncpg, "connect", fake_connect)

    raw = _run(
        canonical_script.build_s1_raw(
            "postgresql://fake-dsn", {"fluid_mechanics": [1], "macroeconomics": [2]}
        )
    )
    assert [r["subject"] for r in raw] == ["fluid_mechanics", "macroeconomics"]
    assert conn.closed is True


def test_dump_writes_expected_payload(tmp_path: Path):
    result = JudgeResult(
        stage="s1_reference_graph",
        verdicts=(Verdict("a", True, ""), Verdict("b", False, "bad")),
        passed=1,
        total=2,
        pass_rate=0.5,
    )
    out_path = tmp_path / "s1-results.json"
    canonical_script.dump(result, out_path)
    payload = json.loads(out_path.read_text())
    assert payload == {
        "stage": "s1_reference_graph",
        "passed": 1,
        "total": 2,
        "pass_rate": 0.5,
        "verdicts": [
            {"item_id": "a", "ok": True, "reason": ""},
            {"item_id": "b", "ok": False, "reason": "bad"},
        ],
    }


class _FakeJudge:
    def __init__(self, result: JudgeResult):
        self._result = result

    def __call__(self, llm):
        return self

    async def judge(self, raw):
        return self._result


def test_run_writes_s1_and_skips_s2_when_no_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    s1_result = JudgeResult(
        stage="s1_reference_graph",
        verdicts=(Verdict("fluid_mechanics:node:eq1", True, ""),),
        passed=1,
        total=1,
        pass_rate=1.0,
    )
    monkeypatch.setattr(canonical_script, "OpenAIJudgeClient", lambda: object())
    monkeypatch.setattr(
        canonical_script, "build_s1_raw", lambda pg_dsn, subjects: _async_return([{"subject": "x"}])
    )
    monkeypatch.setattr(canonical_script, "S1ReferenceGraphJudge", _FakeJudge(s1_result))

    _run(canonical_script.run("dsn", tmp_path, {"fluid_mechanics": [1]}))

    assert json.loads((tmp_path / "s1-results.json").read_text())["stage"] == "s1_reference_graph"
    assert not (tmp_path / "s2-results.json").exists()
    assert "S2 skipped" in capsys.readouterr().out


def test_run_writes_s2_when_fixtures_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s1_result = JudgeResult(
        stage="s1_reference_graph",
        verdicts=(Verdict("fluid_mechanics:node:eq1", True, ""),),
        passed=1,
        total=1,
        pass_rate=1.0,
    )
    s2_result = JudgeResult(
        stage="s2_ingestion",
        verdicts=(Verdict("set1:Problem 1(a)", True, ""),),
        passed=1,
        total=1,
        pass_rate=1.0,
    )
    fixture = {
        "result_summary": {
            "problems": [
                {
                    "label": "Problem 1(a)",
                    "outcome": "promoted",
                    "diagnostic": "ok",
                    "match_method": "exact",
                    "solution_source": "authored",
                    "ocr_confidence": 0.95,
                    "review_required": False,
                }
            ]
        }
    }
    (tmp_path / "authored_set_final1.json").write_text(json.dumps(fixture))

    monkeypatch.setattr(canonical_script, "OpenAIJudgeClient", lambda: object())
    monkeypatch.setattr(
        canonical_script, "build_s1_raw", lambda pg_dsn, subjects: _async_return([{"subject": "x"}])
    )
    monkeypatch.setattr(canonical_script, "S1ReferenceGraphJudge", _FakeJudge(s1_result))
    monkeypatch.setattr(canonical_script, "S2IngestionJudge", _FakeJudge(s2_result))

    _run(canonical_script.run("dsn", tmp_path, {"fluid_mechanics": [1]}))

    assert json.loads((tmp_path / "s2-results.json").read_text())["stage"] == "s2_ingestion"


async def _async_return(value):
    return value


def test_main_parses_args_and_invokes_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured: dict[str, Any] = {}

    async def fake_run(pg_dsn: str, out_dir: Path, subject_concepts: dict[str, list[int]]) -> None:
        captured["pg_dsn"] = pg_dsn
        captured["out_dir"] = out_dir
        captured["subject_concepts"] = subject_concepts

    monkeypatch.setattr(canonical_script, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_s1_s2.py",
            "--out-dir",
            str(tmp_path),
            "--subjects",
            "fluid_mechanics:1",
            "macroeconomics:2,3",
        ],
    )

    canonical_script.main()

    assert captured["pg_dsn"] == canonical_script.DEFAULT_PG_DSN
    assert captured["out_dir"] == tmp_path
    assert captured["subject_concepts"] == {"fluid_mechanics": [1], "macroeconomics": [2, 3]}


def test_canonical_driver_build_s2_raw_reads_authored_set_fixtures(tmp_path: Path):
    module = _load_module("campaign/scripts/run_s1_s2.py", "_test_canonical_run_s1_s2_s2raw2")
    fixture = {
        "result_summary": {
            "problems": [
                {
                    "label": "Problem 1(a)",
                    "outcome": "promoted",
                    "diagnostic": "ok",
                    "match_method": "exact",
                    "solution_source": "authored",
                    "ocr_confidence": 0.95,
                    "review_required": False,
                }
            ]
        }
    }
    (tmp_path / "authored_set_final1.json").write_text(json.dumps(fixture))

    items = module.build_s2_raw(tmp_path)
    assert len(items) == 1
    assert items[0]["item_id"] == "set1:Problem 1(a)"
    assert items[0]["low_confidence_threshold"] is None
    assert items[0]["verify_path_fired"] is False


# --- frozen per-run scripts: must stay pinned to PRECEDES -----------------


@pytest.mark.parametrize(
    "rel_path,module_name",
    [
        ("campaign/out/f1c/run_s1_s2.py", "_test_f1c_run_s1_s2"),
        ("campaign/out/f1/run_s1_s2.py", "_test_f1_run_s1_s2"),
    ],
)
def test_frozen_run_dir_scripts_stay_pinned_to_precedes(rel_path: str, module_name: str):
    module = _load_module(rel_path, module_name)
    nodes, prereqs = _fixture()
    conn = _FakeConn(nodes=nodes, prereqs=prereqs)

    graph = _run(module._fetch_subject_graph(conn, "fluid_mechanics", [1]))

    # These are FROZEN historical run artifacts (see module docstring) --
    # their committed s1-results.json was recorded against PRECEDES-labeled
    # edges, so they must never emit DEPENDS_ON. Land harness fixes in
    # campaign/scripts/run_s1_s2.py (the canonical driver) instead.
    assert graph["edges"] == [
        {
            "edge_type": "PRECEDES",
            "from_node_id": "kinetic_energy_density",
            "to_node_id": "fluid_density",
        }
    ]
    assert "DEPENDS_ON" not in {e["edge_type"] for e in graph["edges"]}
