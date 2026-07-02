"""Unit tests for campaign.cast.materials.generate_fixtures.

Exercises the PDF-pair generator directly (writes into tmp_path, not the
checked-in materials/ directory) so the test suite doesn't depend on or
mutate the committed fixture files.
"""

from __future__ import annotations

import pytest

from campaign.cast.materials.generate_fixtures import (
    LINEAR_MOTION_PROBLEM_TEXT,
    LINEAR_MOTION_SOLUTION_TEXT,
    build_fixture_pdf,
    generate_linear_motion_fixtures,
)

pytestmark = pytest.mark.unit


def test_build_fixture_pdf_writes_a_real_pdf(tmp_path):
    out = tmp_path / "nested" / "note.pdf"

    result = build_fixture_pdf("hello world", out)

    assert result == out
    assert out.is_file()
    assert out.read_bytes().startswith(b"%PDF")


def test_generate_linear_motion_fixtures_writes_both_pdfs(tmp_path):
    problem_path, solution_path = generate_linear_motion_fixtures(out_dir=tmp_path)

    assert problem_path == tmp_path / "linear_motion_problem.pdf"
    assert solution_path == tmp_path / "linear_motion_solution.pdf"
    assert problem_path.read_bytes().startswith(b"%PDF")
    assert solution_path.read_bytes().startswith(b"%PDF")


def test_fixture_text_constants_describe_a_kinematics_problem():
    assert "cyclist" in LINEAR_MOTION_PROBLEM_TEXT
    assert "10.0 m/s" in LINEAR_MOTION_SOLUTION_TEXT
