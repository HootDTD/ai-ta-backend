"""Pure-unit tests for scripts/_macro_probe_report.py (probe report helpers).

Covers the local-DB target guard, the score-matrix extraction from probe
``results`` (incl. missing comparison_runs and errored attempts), and the
fixed-width matrix formatting. No DB, no subprocess, no HTTP.
"""

from __future__ import annotations

import pytest

import scripts._macro_probe_report as rep


# --- local target guard ------------------------------------------------------


def test_db_target_strips_credentials():
    assert rep.db_target("postgresql+asyncpg://u:p@127.0.0.1:54322/postgres") == (
        "127.0.0.1:54322/postgres"
    )
    assert rep.db_target("") == ""


def test_is_local_target():
    assert rep.is_local_target("postgresql+asyncpg://u:p@127.0.0.1:54322/postgres")
    assert rep.is_local_target("postgresql+asyncpg://u:p@localhost:5432/postgres")
    assert not rep.is_local_target("postgresql+asyncpg://u:p@db.supabase.co:5432/postgres")


def test_require_local_target_accepts_local():
    url = "postgresql+asyncpg://u:p@127.0.0.1:54322/postgres"
    assert rep.require_local_target(url) == url
    assert rep.require_local_target("  " + url + "  ") == url  # trimmed


def test_require_local_target_rejects_empty():
    with pytest.raises(rep.LocalTargetError, match="not set"):
        rep.require_local_target("")


def test_require_local_target_rejects_remote():
    remote = "postgresql+asyncpg://u:p@db.uduxdniieeqbljtwocxy.supabase.co:5432/postgres"
    with pytest.raises(rep.LocalTargetError, match="not local"):
        rep.require_local_target(remote)


# --- scores_from_run ---------------------------------------------------------


def test_scores_from_run_none_is_all_none():
    cells = rep.scores_from_run(None)
    assert set(cells) == set(rep.SCORE_COLUMNS) | {"abstained"}
    assert all(v is None for v in cells.values())


def test_scores_from_run_projects_columns():
    run = {col: float(i) for i, col in enumerate(rep.SCORE_COLUMNS)}
    run["abstained"] = True
    run["irrelevant"] = "dropped"
    cells = rep.scores_from_run(run)
    assert cells["coverage_score"] == 0.0
    assert cells["abstained"] is True
    assert "irrelevant" not in cells


# --- build_score_matrix ------------------------------------------------------


def _result(problem, variation, attempt_id, runs=None, error=None):
    out = {
        "served_problem": problem,
        "variation": variation,
        "attempt_id": attempt_id,
        "graphsim_evidence": {"comparison_runs": runs or []},
    }
    if error is not None:
        out["error"] = error
    return out


def test_build_score_matrix_uses_first_run():
    results = [
        _result("gdp_identity", "strong", 1, runs=[
            {"coverage_score": 1.0, "usage_score": 0.5, "abstained": False},
            {"coverage_score": 0.0},  # second run ignored
        ]),
    ]
    matrix = rep.build_score_matrix(results)
    assert len(matrix) == 1
    row = matrix[0]
    assert row["problem"] == "gdp_identity"
    assert row["variation"] == "strong"
    assert row["attempt_id"] == 1
    assert row["coverage_score"] == 1.0
    assert row["usage_score"] == 0.5
    assert row["abstained"] is False


def test_build_score_matrix_missing_runs_yields_none_cells():
    matrix = rep.build_score_matrix([_result("nnp_chain", "weak", 9, runs=[])])
    row = matrix[0]
    assert row["problem"] == "nnp_chain"
    assert row["coverage_score"] is None
    assert row["abstained"] is None


def test_build_score_matrix_falls_back_to_mode_when_no_variation():
    results = [{
        "served_problem": "real_gdp_growth",
        "mode": "partial",  # legacy key
        "attempt_id": 3,
        "graphsim_evidence": {"comparison_runs": [{"coverage_score": 0.7}]},
    }]
    assert rep.build_score_matrix(results)[0]["variation"] == "partial"


def test_build_score_matrix_keeps_errored_attempt():
    results = [_result("real_gdp_from_deflator", "strong", None,
                       runs=[], error="from_hoot 409: ...")]
    row = rep.build_score_matrix(results)[0]
    assert row["error"].startswith("from_hoot 409")
    assert row["coverage_score"] is None


# --- format_score_matrix -----------------------------------------------------


def test_format_score_matrix_has_header_and_rows():
    matrix = rep.build_score_matrix([
        _result("gdp_identity", "strong", 1,
                runs=[{"coverage_score": 1.0, "usage_score": 0.0, "abstained": False}]),
        _result("nnp_chain", "weak", 2, runs=[]),
    ])
    text = rep.format_score_matrix(matrix)
    lines = text.splitlines()
    assert lines[0].startswith("problem")
    assert "usage" in lines[0]
    assert any("gdp_identity" in line for line in lines)
    assert any("nnp_chain" in line for line in lines)


def test_format_score_matrix_renders_cell_types():
    matrix = [{
        "problem": "q", "variation": "strong",
        "coverage_score": 0.5, "usage_score": None, "abstained": True,
    }]
    text = rep.format_score_matrix(matrix)
    assert "0.50" in text  # float rounded
    assert " . " in text or text.endswith(".") or "  . " in text  # None -> "."
    assert "T" in text  # bool abstained -> T


def test_format_score_matrix_empty_matrix_just_header():
    text = rep.format_score_matrix([])
    lines = text.splitlines()
    assert len(lines) == 2  # header + separator only


def test_fmt_cell_stringifies_non_numeric():
    # a non-None, non-bool, non-numeric cell falls through to str()
    assert rep._fmt_cell("oops") == "oops"
    assert rep._fmt_cell(None) == "."
    assert rep._fmt_cell(True) == "T"
    assert rep._fmt_cell(False) == "F"
    assert rep._fmt_cell(0.5) == "0.50"
