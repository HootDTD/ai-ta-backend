"""Unit tests for scripts/run_macro_probe.py (macro probe orchestrator).

The orchestrator is integration-shaped (subprocess + uvicorn + DB). These tests
pin the PURE helpers (env prep + local guard, command builders, report wiring)
and the ``main`` control flow with EVERY impure boundary faked: ``_run`` and
``subprocess.Popen`` (no child processes), ``_wait_for_port`` (no sockets), the
two async DB probes (``_macro_space_id`` / ``_corpus_embedded``), and the report
files. No real DB, no real subprocess, no real server.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.run_macro_probe as o
from scripts._macro_probe_report import LocalTargetError

LOCAL_URL = "postgresql+asyncpg://u:p@127.0.0.1:54322/postgres"
REMOTE_URL = "postgresql+asyncpg://u:p@db.supabase.co:5432/postgres"


# --- prepare_env -------------------------------------------------------------


def test_prepare_env_forces_db_and_web_base():
    env = o.prepare_env({"SUPABASE_DB_URL": LOCAL_URL, "KEEP": "1"})
    assert env["DATABASE_URL"] == LOCAL_URL
    assert env["SUPABASE_DB_URL"] == LOCAL_URL
    assert env["WEB_BASE_URL"] == o.PROBE_BASE_URL
    assert env["KEEP"] == "1"


def test_prepare_env_does_not_mutate_input():
    src = {"SUPABASE_DB_URL": LOCAL_URL}
    o.prepare_env(src)
    assert "DATABASE_URL" not in src
    assert "WEB_BASE_URL" not in src


def test_prepare_env_rejects_remote():
    with pytest.raises(LocalTargetError):
        o.prepare_env({"SUPABASE_DB_URL": REMOTE_URL})


def test_prepare_env_rejects_missing():
    with pytest.raises(LocalTargetError):
        o.prepare_env({})


# --- command builders --------------------------------------------------------


def test_seed_commands_order_and_args():
    cmds = o.seed_commands("PY", "DBURL", 3)
    assert cmds[0] == ["PY", "-m", "scripts.seed_apollo_concept_registry", "--database-url", "DBURL"]
    assert cmds[1][:2] == ["PY", "scripts/seed_apollo_learner_model.py"]
    assert "--subject-slug" in cmds[1] and o.SUBJECT_SLUG in cmds[1]
    # learner-model + canon MUST be scoped to the macro course (search_space_id)
    assert "--search-space-id" in cmds[1] and "3" in cmds[1]
    assert cmds[2][:2] == ["PY", "scripts/seed_canon_projection.py"]
    assert "--search-space-id" in cmds[2] and "3" in cmds[2]


def test_index_command():
    cmd = o.index_command("PY", "/x/ch6.pdf", 7)
    assert cmd[:4] == ["PY", "scripts/index_local_pdf.py", "--pdf", "/x/ch6.pdf"]
    assert "--search-space-id" in cmd and "7" in cmd
    assert "--material-kind" in cmd and "textbook" in cmd
    assert "--week" in cmd and "none" in cmd


def test_probe_command_with_tag():
    cmd = o.probe_command("PY", ".macro1")
    assert "--macro" in cmd
    assert "--subject-slug" in cmd and o.SUBJECT_SLUG in cmd
    assert "strong,partial,weak" in cmd
    assert cmd[-2:] == ["--tag", ".macro1"]


def test_probe_command_without_tag_omits_tag_flag():
    assert "--tag" not in o.probe_command("PY", "")


def test_server_command_targets_probe_port():
    cmd = o.server_command("PY")
    assert cmd[:2] == ["PY", "-c"]
    assert "server:app" in cmd[2]
    assert f"port={o.PROBE_PORT}" in cmd[2]
    assert o.PROBE_HOST in cmd[2]


# --- report wiring -----------------------------------------------------------


def test_load_report_missing_returns_empty(tmp_path):
    assert o.load_report(tmp_path / "nope.json") == {}


def test_load_report_reads_json(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"results": [], "canon_nodes": 3}), encoding="utf-8")
    assert o.load_report(path)["canon_nodes"] == 3


def test_score_matrix_from_report_delegates():
    report = {"results": [
        {"served_problem": "gdp_identity", "variation": "strong", "attempt_id": 1,
         "graphsim_evidence": {"comparison_runs": [{"coverage_score": 1.0}]}},
    ]}
    matrix = o.score_matrix_from_report(report)
    assert matrix[0]["problem"] == "gdp_identity"
    assert matrix[0]["coverage_score"] == 1.0


# --- main() control flow (all impure boundaries faked) ----------------------


def _ok(_cmd, _env):
    return SimpleNamespace(returncode=0)


def _patch_dotenv():
    return patch.object(o, "load_dotenv", lambda *_a, **_k: None)


def _common_patches(*, embedded: bool, run_side=_ok):
    """Patch every impure boundary main() touches. Returns the _run MagicMock.

    The async DB probes are patched with ``AsyncMock`` (they are ``async def``,
    so ``asyncio.run`` awaits them correctly).
    """
    run_mock = MagicMock(side_effect=run_side)
    popen = MagicMock(return_value=SimpleNamespace(
        terminate=MagicMock(), wait=MagicMock(), kill=MagicMock()))
    return run_mock, [
        _patch_dotenv(),
        patch.object(o, "_run", run_mock),
        patch.object(o.subprocess, "Popen", popen),
        patch.object(o, "_wait_for_port", return_value=True),
        patch.object(o.time, "sleep", lambda *_a: None),
        patch.object(o, "_macro_space_id", AsyncMock(return_value=3)),
        patch.object(o, "_corpus_embedded", AsyncMock(return_value=embedded)),
    ]


def _run_main(argv, run_mock, patches, *, report=None):
    """Run main() under the patches, faking load_report/format/write."""
    report = report or {"canon_nodes": 1, "web_base": o.PROBE_BASE_URL, "results": []}
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
        patch.object(o, "load_report", return_value=report), \
        patch.object(Path, "write_text", MagicMock()), \
        patch.dict(o.os.environ, {"SUPABASE_DB_URL": LOCAL_URL}, clear=True):
        return o.main(argv)


def test_main_skips_embed_when_corpus_present(monkeypatch):
    run_mock, patches = _common_patches(embedded=True)
    code = _run_main(["--skip-mining", "--tag", ".t"], run_mock, patches)
    assert code == 0
    # index_local_pdf is NOT among the _run calls when corpus already embedded
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert not any("index_local_pdf.py" in r for r in ran)
    # the 3 seeds + the probe DID run
    assert any("seed_apollo_concept_registry" in r for r in ran)
    assert any("seed_apollo_learner_model.py" in r for r in ran)
    assert any("seed_canon_projection.py" in r for r in ran)
    assert any("apollo_grade_probe.py" in r for r in ran)


def test_main_embeds_when_corpus_missing_and_pdf_given():
    run_mock, patches = _common_patches(embedded=False)
    code = _run_main(["--skip-mining", "--pdf", "/x/ch6.pdf"], run_mock, patches)
    assert code == 0
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert any("index_local_pdf.py" in r and "/x/ch6.pdf" in r for r in ran)


def test_main_aborts_when_corpus_missing_and_no_pdf():
    run_mock, patches = _common_patches(embedded=False)
    code = _run_main(["--skip-mining"], run_mock, patches)
    assert code == 1
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert not any("seed_" in r for r in ran)  # bailed before seeding


def test_main_aborts_when_seed_fails():
    def _fail_on_canon(cmd, _env):
        rc = 1 if "seed_canon_projection.py" in " ".join(cmd) else 0
        return SimpleNamespace(returncode=rc)

    run_mock, patches = _common_patches(embedded=True, run_side=_fail_on_canon)
    code = _run_main(["--skip-mining"], run_mock, patches)
    assert code == 1
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert not any("apollo_grade_probe.py" in r for r in ran)  # never reached the probe


def test_main_rejects_remote_db():
    with _patch_dotenv(), \
        patch.dict(o.os.environ, {"SUPABASE_DB_URL": REMOTE_URL}, clear=True):
        assert o.main(["--skip-mining", "--skip-embed"]) == 2


def test_main_runs_mining_when_not_skipped():
    run_mock, patches = _common_patches(embedded=True)
    with patch.object(o, "_run_mining", return_value=0) as mine:
        code = _run_main(["--skip-embed"], run_mock, patches)
    assert code == 0
    mine.assert_called_once()


def test_main_skip_mining_does_not_call_mining():
    run_mock, patches = _common_patches(embedded=True)
    with patch.object(o, "_run_mining", return_value=0) as mine:
        _run_main(["--skip-mining", "--skip-embed"], run_mock, patches)
    mine.assert_not_called()


def test_main_continues_when_server_boots_and_probe_fails():
    def _fail_probe(cmd, _env):
        rc = 1 if "apollo_grade_probe.py" in " ".join(cmd) else 0
        return SimpleNamespace(returncode=rc)

    run_mock, patches = _common_patches(embedded=True, run_side=_fail_probe)
    # probe non-zero is a warning, not a failure — main still writes the matrix (rc 0)
    code = _run_main(["--skip-mining", "--skip-embed"], run_mock, patches)
    assert code == 0


def test_main_aborts_when_embed_fails():
    def _fail_embed(cmd, _env):
        rc = 1 if "index_local_pdf.py" in " ".join(cmd) else 0
        return SimpleNamespace(returncode=rc)

    run_mock, patches = _common_patches(embedded=False, run_side=_fail_embed)
    code = _run_main(["--skip-mining", "--pdf", "/x/ch6.pdf"], run_mock, patches)
    assert code == 1
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert not any("seed_" in r for r in ran)  # bailed before seeding


def test_main_aborts_when_server_does_not_boot():
    run_mock, patches = _common_patches(embedded=True)
    # override _wait_for_port -> False (server never comes up)
    patches[3] = patch.object(o, "_wait_for_port", return_value=False)
    code = _run_main(["--skip-mining", "--skip-embed"], run_mock, patches)
    assert code == 1
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert not any("apollo_grade_probe.py" in r for r in ran)  # probe never ran


def test_main_warns_and_continues_when_mining_nonzero():
    run_mock, patches = _common_patches(embedded=True)
    with patch.object(o, "_run_mining", return_value=3):
        code = _run_main(["--skip-embed"], run_mock, patches)
    assert code == 0  # mining failure is a warning, not fatal
    ran = [" ".join(c.args[0]) for c in run_mock.call_args_list]
    assert any("apollo_grade_probe.py" in r for r in ran)  # probe still ran


# --- _run_mining: driver present vs absent ----------------------------------


def test_run_mining_noop_without_driver(tmp_path):
    # Point ROOT at an empty dir so _macro_mine.py does not exist.
    with patch.object(o, "ROOT", tmp_path):
        rc = o._run_mining(LOCAL_URL, 3, {"X": "1"})
    assert rc == 0


def test_run_mining_invokes_driver_when_present(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "_macro_mine.py").write_text("# stub\n", encoding="utf-8")
    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0))
    with patch.object(o, "ROOT", tmp_path), patch.object(o, "_run", run_mock):
        rc = o._run_mining(LOCAL_URL, 7, {"X": "1"})
    assert rc == 0
    cmd = run_mock.call_args.args[0]
    assert "_macro_mine.py" in " ".join(cmd)
    assert "--search-space-id" in cmd and "7" in cmd
