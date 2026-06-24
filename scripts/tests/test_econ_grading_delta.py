"""Structural + invariant tests for the no-infra econ grading-delta harness.

Exercises ``run_delta()`` (the importable entrypoint) so the harness body is
covered. Asserts the metric structure plus the BEFORE invariant: the rearranged
deflator equation lands ``unresolved`` on Phase-0 code (the sign-exact symbolic
tier rejects it). This invariant is FLIPPED to the AFTER form once the Phase-1a
``derived`` tier lands (Step F).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The harness lives in scripts/ (a non-package dir). Put it on sys.path so it is
# importable by name regardless of the pytest rootdir / import mode.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import econ_grading_delta  # noqa: E402  (path-dependent import, see above)


def _rearranged(out: dict) -> dict:
    """The per_node entry for the rearranged deflator equation under test."""
    matches = [n for n in out["per_node"] if n["content"] == "realGDP - nomGDP/(PI/100)"]
    assert len(matches) == 1, f"expected exactly one rearranged node, got {matches}"
    return matches[0]


def test_run_delta_structure():
    out = econ_grading_delta.run_delta()

    assert set(out) == {
        "per_node",
        "unresolved_rate",
        "dropped_edge_count",
        "sub_scores",
    }
    assert isinstance(out["per_node"], list)
    for entry in out["per_node"]:
        assert set(entry) == {"content", "resolution", "method"}
        assert isinstance(entry["content"], str)
        assert isinstance(entry["resolution"], str)
        assert isinstance(entry["method"], str)
    assert isinstance(out["unresolved_rate"], float)
    assert isinstance(out["dropped_edge_count"], int)
    assert set(out["sub_scores"]) == {
        "coverage",
        "node_coverage",
        "edge_coverage",
        "scoping",
        "usage",
    }
    for value in out["sub_scores"].values():
        assert isinstance(value, float)


def test_base_equation_control_resolves_exact():
    """The sign-exact base form is the control: it resolves (via the exact tier,
    the student surface == the reference ``symbolic`` verbatim) BEFORE and AFTER."""
    out = econ_grading_delta.run_delta()
    base = [n for n in out["per_node"] if n["content"] == "deflator - (nomGDP/realGDP)*100"]
    assert len(base) == 1
    assert base[0]["resolution"] == "resolved"
    assert base[0]["method"] == "exact"


# The BEFORE edge_coverage on Phase-0 base (rearranged eq unresolved -> USES edge
# dropped). The AFTER value must be strictly greater (the derived tier retains the
# edge). See the captured # BEFORE / # AFTER blocks in econ_grading_delta.py.
_BEFORE_EDGE_COVERAGE = 0.0


def test_after_rearranged_equation_resolves_via_derived():
    """AFTER invariant (Phase-1a code): the rearranged form resolves through the
    derived tier, the USES edge survives, and edge_coverage rises vs BEFORE."""
    out = econ_grading_delta.run_delta()
    rearranged = _rearranged(out)
    assert rearranged["resolution"] == "resolved"
    assert rearranged["method"] == "derived"
    # The USES edge into the now-resolved equation endpoint is retained.
    assert out["dropped_edge_count"] == 0
    # edge_coverage strictly up vs the BEFORE snapshot (0.0 -> > 0.0).
    assert out["sub_scores"]["edge_coverage"] > _BEFORE_EDGE_COVERAGE
