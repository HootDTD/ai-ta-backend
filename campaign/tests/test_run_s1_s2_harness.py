"""Regression tests for the S1 raw-graph harness scripts.

The CANONICAL driver (``campaign/scripts/run_s1_s2.py``) must emit
``DEPENDS_ON`` for every ``apollo_entity_prereqs`` row -- per
``apollo/ontology/edges.py``, PRECEDES is legal ONLY between
``(procedure_step, procedure_step)`` pairs; generic concept->concept
prerequisite links must be DEPENDS_ON (see
``docs/_archive/experiments/2026-07-03-s1-judge-adjudication.md`` sec 2A:
this mislabel drove 26 of 57 S1 failures in the f1/f1c campaign runs).
Land all future S1/S2 harness changes in this canonical script.

(The frozen per-run scripts under ``campaign/out/f1/`` and
``campaign/out/f1c/`` that this suite used to pin byte-faithful to
PRECEDES were deleted as run-artifact residue in the 2026-07-16 repo
cleanup -- they were historical outputs, not live code, and are
regenerable from the canonical driver above.)

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

import asyncpg
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

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("page-evidence/run-id fetch must not run when there are no fixtures")

    monkeypatch.setattr(canonical_script, "_fetch_page_evidence", _fail_if_called)
    monkeypatch.setattr(canonical_script, "_fetch_run_ids_by_document", _fail_if_called)

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
        "problem_document_id": 7,
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
        },
    }
    (tmp_path / "authored_set_final1.json").write_text(json.dumps(fixture))

    monkeypatch.setattr(canonical_script, "OpenAIJudgeClient", lambda: object())
    monkeypatch.setattr(
        canonical_script, "build_s1_raw", lambda pg_dsn, subjects: _async_return([{"subject": "x"}])
    )
    monkeypatch.setattr(canonical_script, "S1ReferenceGraphJudge", _FakeJudge(s1_result))
    monkeypatch.setattr(canonical_script, "S2IngestionJudge", _FakeJudge(s2_result))
    monkeypatch.setattr(
        canonical_script,
        "_fetch_run_ids_by_document",
        lambda pg_dsn, document_ids: _async_return({7: 42}),
    )
    monkeypatch.setattr(
        canonical_script,
        "_fetch_page_evidence",
        lambda pg_dsn: _async_return({"42": {"problem": "the real scraped page text"}}),
    )

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


def _problem_fixture(**overrides: Any) -> dict:
    problem = {
        "label": "Problem 1(a)",
        "outcome": "promoted",
        "diagnostic": "ok",
        "match_method": "exact",
        "solution_source": "authored",
        "ocr_confidence": 0.95,
        "review_required": False,
    }
    problem.update(overrides)
    return {"result_summary": {"problems": [problem]}}


def test_build_s2_raw_resolves_evidence_via_real_document_run_linkage(tmp_path: Path):
    # Two authored sets whose document ids do NOT map to sequential run ids
    # (as would be the case after a failed ingest consumed a run -- PR #90).
    # If the harness fell back to positional set-N == run-N pairing, set 1
    # would wrongly pick up run "1" evidence instead of its real run "42".
    fixture1 = {"problem_document_id": 7, **_problem_fixture()}
    fixture2 = {"problem_document_id": 9, **_problem_fixture(label="Problem 2(a)")}
    (tmp_path / "authored_set_final1.json").write_text(json.dumps(fixture1))
    (tmp_path / "authored_set_final2.json").write_text(json.dumps(fixture2))

    page_evidence = {
        "42": {"problem": "real page text for document 7"},
        "1": {"problem": "WRONG -- positional trap, must not be picked up"},
    }
    run_id_by_document = {7: 42, 9: 99}  # document 9's run (99) has no evidence

    items = canonical_script.build_s2_raw(tmp_path, page_evidence, run_id_by_document)

    assert len(items) == 2
    set1_item = next(i for i in items if i["item_id"] == "set1:Problem 1(a)")
    assert set1_item["paired_solution"]["source_page_ocr"] == {
        "problem": "real page text for document 7"
    }
    set2_item = next(i for i in items if i["item_id"] == "set2:Problem 2(a)")
    assert "source_page_ocr" not in set2_item["paired_solution"]


def test_build_s2_raw_skips_evidence_when_fixture_has_no_document_id(tmp_path: Path):
    fixture = _problem_fixture()  # no problem_document_id key at all
    (tmp_path / "authored_set_final1.json").write_text(json.dumps(fixture))

    items = canonical_script.build_s2_raw(
        tmp_path, {"1": {"problem": "should not be attached"}}, {}
    )

    assert "source_page_ocr" not in items[0]["paired_solution"]


def test_document_ids_from_fixtures_dedupes_and_skips_missing(tmp_path: Path):
    (tmp_path / "authored_set_final1.json").write_text(
        json.dumps({"problem_document_id": 5, **_problem_fixture()})
    )
    (tmp_path / "authored_set_final2.json").write_text(
        json.dumps({"problem_document_id": 5, **_problem_fixture()})
    )
    (tmp_path / "authored_set_final3.json").write_text(json.dumps(_problem_fixture()))

    paths = sorted(tmp_path.glob("authored_set_final*.json"))
    assert canonical_script._document_ids_from_fixtures(paths) == [5]


class _FakePageEvidenceConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, missing_table: bool = False):
        self._rows = rows or []
        self._missing_table = missing_table
        self.closed = False

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if self._missing_table:
            raise asyncpg.UndefinedTableError("missing table")
        return self._rows

    async def close(self) -> None:
        self.closed = True


def test_fetch_page_evidence_happy_path_concatenates_pages_per_role(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [
        {"ingest_run_id": 42, "role": "problem", "page_number": 1, "ocr_text": "page one"},
        {"ingest_run_id": 42, "role": "problem", "page_number": 2, "ocr_text": "page two"},
        {"ingest_run_id": 42, "role": "solution", "page_number": 1, "ocr_text": "sol page"},
    ]
    conn = _FakePageEvidenceConn(rows=rows)

    async def fake_connect(dsn: str):
        return conn

    monkeypatch.setattr(canonical_script.asyncpg, "connect", fake_connect)

    evidence = _run(canonical_script._fetch_page_evidence("dsn"))

    assert evidence == {
        "42": {"problem": "page one\npage two", "solution": "sol page"},
    }
    assert conn.closed is True


def test_fetch_page_evidence_returns_empty_when_table_missing_pre_036(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakePageEvidenceConn(missing_table=True)

    async def fake_connect(dsn: str):
        return conn

    monkeypatch.setattr(canonical_script.asyncpg, "connect", fake_connect)

    evidence = _run(canonical_script._fetch_page_evidence("dsn"))

    assert evidence == {}
    assert conn.closed is True


def test_fetch_run_ids_by_document_returns_empty_for_empty_input():
    assert _run(canonical_script._fetch_run_ids_by_document("dsn", [])) == {}


def test_fetch_run_ids_by_document_maps_latest_run_per_document(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [{"document_id": 7, "id": 42}, {"document_id": 9, "id": 99}]
    conn = _FakePageEvidenceConn(rows=rows)

    async def fake_connect(dsn: str):
        return conn

    monkeypatch.setattr(canonical_script.asyncpg, "connect", fake_connect)

    result = _run(canonical_script._fetch_run_ids_by_document("dsn", [7, 9]))

    assert result == {7: 42, 9: 99}
    assert conn.closed is True
