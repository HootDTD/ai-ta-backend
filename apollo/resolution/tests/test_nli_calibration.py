"""Tests for apollo/resolution/calibration.py and scripts/apollo_nli_calibrate.py.

Covers:
- sweep_thresholds / best_operating_point with NLIResult-returning fake classifiers
- load_dev_set round-trip + bad-gold error path
- format_report non-empty table with selected thresholds
- main() exit code 0 (bar reachable) and exit code 1 (bar unreachable)
  driven through the injectable adjudicator parameter
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from apollo.resolution.calibration import (
    MAX_CONTRADICTION_GRID,
    MIN_ENTAILMENT_GRID,
    GridPoint,
    LabeledPair,
    SweepReport,
    best_operating_point,
    format_report,
    load_dev_set,
    sweep_thresholds,
)
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult
from apollo.resolution.nli_config import NLIParams

# ---------------------------------------------------------------------------
# Load scripts/apollo_nli_calibrate.py by file path so the test can call
# main() directly without requiring scripts/ to be a package.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
_SCRIPT_PATH = _SCRIPTS_DIR / "apollo_nli_calibrate.py"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import apollo_nli_calibrate  # noqa: E402  (path-dependent import)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake(mapping: dict[tuple[str, str], NLIResult]) -> FakeNLIAdjudicator:
    return FakeNLIAdjudicator(mapping)


# ---------------------------------------------------------------------------
# Brief-specified tests (corrected to return NLIResult, per Correction 1)
# ---------------------------------------------------------------------------


def test_precision_gate_selects_high_threshold() -> None:
    """When the classifier returns high entailment only for premise==hypothesis,
    every grid point has precision 1.0 (no false positives) and recall 1.0
    (all gold-entailment pairs credited).  best_operating_point should return
    the highest-recall point — which is any of them, resolved by tie-break to
    the highest min_entailment, lowest max_contradiction.  That value must be
    >= 0.6 (minimum grid value).
    """
    labeled: list[Any] = [
        ("a", "a", "entailment"),
        ("b", "c", "neutral"),
        ("d", "d", "entailment"),
    ]

    def fake(premise: str, hypothesis: str) -> NLIResult:
        # High entailment only when premise == hypothesis; neutral otherwise.
        if premise == hypothesis:
            return NLIResult("entailment", 0.99, 0.0, 0.01, "fake")
        return NLIResult("neutral", 0.50, 0.0, 0.50, "fake")

    report = sweep_thresholds(labeled, fake)
    params = best_operating_point(report, min_precision=0.95)
    assert params is not None and params.min_entailment >= 0.6


def test_returns_none_when_bar_unreachable() -> None:
    """Only a false-positive pair: every grid point has precision 0.0 (one FP,
    zero TP), so no point meets the 0.95 floor — best_operating_point → None.
    """
    labeled: list[Any] = [("a", "b", "neutral")]  # the only pair is a neutral

    def fake(p: str, h: str) -> NLIResult:
        # Always reports high entailment — every pair gets credited as FP.
        return NLIResult("entailment", 0.99, 0.0, 0.01, "fake")

    assert best_operating_point(sweep_thresholds(labeled, fake), min_precision=0.95) is None


# ---------------------------------------------------------------------------
# sweep_thresholds — structural invariants
# ---------------------------------------------------------------------------


def test_sweep_report_has_correct_grid_size() -> None:
    """The report must have one GridPoint per (min_ent, max_con) combination."""
    labeled = [LabeledPair("p", "h", "entailment")]
    fake = lambda p, h: NLIResult("entailment", 0.99, 0.0, 0.01, "fake")  # noqa: E731
    report = sweep_thresholds(labeled, fake)
    assert len(report) == len(MIN_ENTAILMENT_GRID) * len(MAX_CONTRADICTION_GRID)


def test_sweep_accepts_bare_tuples() -> None:
    """sweep_thresholds must work with plain 3-tuples (not just LabeledPair)."""
    labeled: list[Any] = [("p", "h", "entailment")]
    fake = lambda p, h: NLIResult("entailment", 0.99, 0.0, 0.01, "fake")  # noqa: E731
    report = sweep_thresholds(labeled, fake)
    # At the lowest thresholds the pair is credited: TP=1, FP=0 → precision=1.0
    assert report[0].tp == 1
    assert report[0].fp == 0


def test_precision_is_one_when_no_credit_issued() -> None:
    """A threshold too strict to credit anything → TP=FP=0 → precision=1.0."""
    labeled: list[Any] = [("p", "h", "entailment")]
    # Classifier returns entailment=0.0, which will fail every min_entailment.
    fake = lambda p, h: NLIResult("entailment", 0.0, 0.0, 1.0, "fake")  # noqa: E731
    report = sweep_thresholds(labeled, fake)
    for pt in report:
        assert pt.tp == 0
        assert pt.fp == 0
        assert pt.precision == 1.0


def test_recall_is_zero_when_no_gold_entailment_pairs() -> None:
    """Dev set with only neutral pairs → recall 0.0 at all grid points."""
    labeled: list[Any] = [("p", "h", "neutral"), ("x", "y", "contradiction")]
    fake = lambda p, h: NLIResult("neutral", 0.1, 0.1, 0.8, "fake")  # noqa: E731
    report = sweep_thresholds(labeled, fake)
    for pt in report:
        assert pt.recall == 0.0


def test_tp_fp_fn_counts_are_correct() -> None:
    """Manual verification of TP/FP/FN at one specific grid point."""
    # 3 pairs: 2 entailment, 1 neutral.
    # Classifier: label=entailment with ent=0.80, con=0.05.
    # At (min_ent=0.75, max_con=0.10): all three get credited.
    # TP=2 (the two gold-entailment), FP=1 (the gold-neutral), FN=0.
    labeled: list[Any] = [
        ("a", "b", "entailment"),
        ("c", "d", "entailment"),
        ("e", "f", "neutral"),
    ]
    fake = lambda p, h: NLIResult("entailment", 0.80, 0.05, 0.15, "fake")  # noqa: E731
    report = sweep_thresholds(labeled, fake)
    # Find the (0.75, 0.10) grid point.
    pt = next(p for p in report if p.min_entailment == 0.75 and p.max_contradiction == 0.10)
    assert pt.tp == 2
    assert pt.fp == 1
    assert pt.fn == 0
    assert pytest.approx(pt.precision) == 2 / 3
    assert pt.recall == 1.0


# ---------------------------------------------------------------------------
# best_operating_point
# ---------------------------------------------------------------------------


def test_best_op_selects_highest_recall_above_floor() -> None:
    """Among points at precision >= 0.95, the highest recall is chosen."""
    # Two grid points for illustration — we build a minimal synthetic report.
    low_recall = GridPoint(0.85, 0.10, 0.99, 0.50, 0.66, 5, 0, 5)
    high_recall = GridPoint(0.80, 0.10, 0.97, 0.90, 0.93, 9, 0, 1)
    below_floor = GridPoint(0.70, 0.20, 0.80, 0.95, 0.87, 10, 2, 0)
    report: SweepReport = [low_recall, high_recall, below_floor]
    p = best_operating_point(report, min_precision=0.95)
    assert p is not None
    assert p.min_entailment == 0.80
    assert p.max_contradiction == 0.10


def test_best_op_tiebreak_prefers_higher_min_entailment() -> None:
    """Tie on recall: higher min_entailment wins."""
    pt_a = GridPoint(0.80, 0.10, 1.0, 0.80, 0.89, 8, 0, 2)
    pt_b = GridPoint(0.85, 0.10, 1.0, 0.80, 0.89, 8, 0, 2)
    report: SweepReport = [pt_a, pt_b]
    p = best_operating_point(report, min_precision=0.95)
    assert p is not None
    assert p.min_entailment == 0.85


def test_best_op_tiebreak_prefers_lower_max_contradiction() -> None:
    """Tie on recall AND min_entailment: lower max_contradiction wins."""
    pt_a = GridPoint(0.85, 0.20, 1.0, 0.80, 0.89, 8, 0, 2)
    pt_b = GridPoint(0.85, 0.05, 1.0, 0.80, 0.89, 8, 0, 2)
    report: SweepReport = [pt_a, pt_b]
    p = best_operating_point(report, min_precision=0.95)
    assert p is not None
    assert p.max_contradiction == 0.05


def test_best_op_constructs_nliparams_with_other_defaults() -> None:
    """The returned NLIParams must use NLIParams defaults for non-swept fields."""
    pt = GridPoint(0.85, 0.10, 1.0, 0.80, 0.89, 8, 0, 2)
    defaults = NLIParams()
    p = best_operating_point([pt], min_precision=0.95)
    assert p is not None
    assert p.min_entailment == 0.85
    assert p.max_contradiction == 0.10
    assert p.top_k == defaults.top_k
    assert p.ambiguity_margin == defaults.ambiguity_margin
    assert p.misconception_veto_entailment == defaults.misconception_veto_entailment


def test_best_op_returns_none_when_all_below_floor() -> None:
    low = GridPoint(0.85, 0.10, 0.80, 0.90, 0.85, 9, 2, 1)
    assert best_operating_point([low], min_precision=0.95) is None


# ---------------------------------------------------------------------------
# load_dev_set
# ---------------------------------------------------------------------------


def test_load_dev_set_round_trip(tmp_path: Path) -> None:
    """Write a small JSONL file and load it back; verify all fields."""
    rows = [
        {"premise": "p1", "hypothesis": "h1", "gold": "entailment", "source": "test"},
        {"premise": "p2", "hypothesis": "h2", "gold": "neutral"},
        {"premise": "p3", "hypothesis": "h3", "gold": "contradiction", "category": "x"},
    ]
    f = tmp_path / "dev.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    pairs = load_dev_set(f)
    assert len(pairs) == 3
    assert pairs[0] == LabeledPair("p1", "h1", "entailment")
    assert pairs[1] == LabeledPair("p2", "h2", "neutral")
    assert pairs[2] == LabeledPair("p3", "h3", "contradiction")


def test_load_dev_set_ignores_blank_lines(tmp_path: Path) -> None:
    content = (
        '{"premise":"p","hypothesis":"h","gold":"entailment"}\n'
        "\n"
        '{"premise":"p2","hypothesis":"h2","gold":"neutral"}\n'
    )
    f = tmp_path / "dev.jsonl"
    f.write_text(content, encoding="utf-8")
    pairs = load_dev_set(f)
    assert len(pairs) == 2


def test_load_dev_set_raises_on_bad_gold(tmp_path: Path) -> None:
    """A line with an invalid gold label must raise ValueError."""
    f = tmp_path / "bad.jsonl"
    f.write_text(
        '{"premise":"p","hypothesis":"h","gold":"ENTAILMENT"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid gold label"):
        load_dev_set(f)


def test_load_dev_set_loads_committed_dev_set() -> None:
    """The committed nli_dev_set.jsonl must load without errors."""
    dev_set_path = Path(__file__).parent / "data" / "nli_dev_set.jsonl"
    pairs = load_dev_set(dev_set_path)
    # Sanity: non-empty and all golds are valid.
    assert len(pairs) > 100
    from apollo.resolution.calibration import VALID_GOLD_LABELS

    for p in pairs:
        assert p.gold in VALID_GOLD_LABELS


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_non_empty_with_selected(tmp_path: Path) -> None:
    """format_report must produce a table and include the selected thresholds."""
    pt = GridPoint(0.85, 0.10, 1.0, 0.80, 0.89, 8, 0, 2)
    selected = NLIParams(min_entailment=0.85, max_contradiction=0.10)
    out = format_report([pt], selected)
    assert len(out) > 0
    assert "0.85" in out
    assert "0.10" in out
    assert "min_ent" in out  # header present


def test_format_report_says_none_when_unreachable() -> None:
    pt = GridPoint(0.85, 0.10, 0.80, 0.90, 0.85, 9, 2, 1)
    out = format_report([pt], None)
    assert "None" in out


def test_format_report_includes_full_grid() -> None:
    """Calling format_report with a full sweep report must include all grid rows."""
    labeled: list[Any] = [("p", "h", "entailment")]
    fake = lambda p, h: NLIResult("entailment", 0.99, 0.0, 0.01, "fake")  # noqa: E731
    report = sweep_thresholds(labeled, fake)
    selected = best_operating_point(report, min_precision=0.95)
    out = format_report(report, selected)
    # One row per grid point (32 total) plus header + separator + blank + footer
    lines = out.splitlines()
    assert len(lines) > len(MIN_ENTAILMENT_GRID) * len(MAX_CONTRADICTION_GRID)
    assert "Selected: min_entailment=" in out


# ---------------------------------------------------------------------------
# main() via the injectable adjudicator — covers apollo_nli_calibrate
# ---------------------------------------------------------------------------


def _tmp_jsonl(tmp_path: Path, rows: list[dict]) -> Path:
    f = tmp_path / "dev.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return f


def test_main_returns_0_when_precision_floor_reachable(tmp_path: Path) -> None:
    """main() should return 0 when a best operating point exists."""
    # 2 entailment pairs, 0 false positives — precision 1.0 everywhere.
    rows = [
        {"premise": "a", "hypothesis": "a", "gold": "entailment"},
        {"premise": "b", "hypothesis": "b", "gold": "entailment"},
    ]
    f = _tmp_jsonl(tmp_path, rows)
    adj = FakeNLIAdjudicator(
        {
            ("a", "a"): NLIResult("entailment", 0.99, 0.0, 0.01, "fake"),
            ("b", "b"): NLIResult("entailment", 0.99, 0.0, 0.01, "fake"),
        }
    )
    rc = apollo_nli_calibrate.main(argv=[str(f)], adjudicator=adj)
    assert rc == 0


def test_main_returns_nonzero_when_precision_floor_unreachable(tmp_path: Path) -> None:
    """main() should return non-zero when no grid point meets 0.95 precision."""
    # One neutral pair always gets credited → precision 0.0 everywhere.
    rows = [{"premise": "x", "hypothesis": "y", "gold": "neutral"}]
    f = _tmp_jsonl(tmp_path, rows)
    adj = FakeNLIAdjudicator({("x", "y"): NLIResult("entailment", 0.99, 0.0, 0.01, "fake")})
    rc = apollo_nli_calibrate.main(argv=[str(f)], adjudicator=adj)
    assert rc != 0


def test_main_prints_report(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
    """main() must print the formatted report to stdout."""
    rows = [{"premise": "p", "hypothesis": "h", "gold": "entailment"}]
    f = _tmp_jsonl(tmp_path, rows)
    adj = FakeNLIAdjudicator({("p", "h"): NLIResult("entailment", 0.99, 0.0, 0.01, "fake")})
    apollo_nli_calibrate.main(argv=[str(f)], adjudicator=adj)
    captured = capsys.readouterr()
    assert "min_ent" in captured.out
    assert "Selected:" in captured.out
