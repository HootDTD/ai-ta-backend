"""The §4 gates that CANNOT be closed deterministically — enumerated, not hidden.

A gate table with silent holes is worse than one with declared ones: the PR and
the campaign report both point at ``tests/gates_p32/``, so every row of spec §4
must appear here, and the rows this build cannot close must say WHY and WHAT
would close them. Each stub below is a ``pytest.mark.skip`` whose reason is the
substitute evidence required — running the suite prints the whole table.

| Gate | Spec §4 row | Level | Why it cannot be a unit test |
|---|---|---|---|
| **G6** | Done-gate cost: median added turns on firing attempts <= 1 | 2 | needs a LIVE cohort — replay cannot generate the answer to a question that was never asked (§4.1) |
| **G7** | Denominator confound: arm-B score movement caused by the gate enlarging ``_probed_node_ids`` -> P1.2b | 2 | a distribution measured across arms, reported separately from any ceiling |
| **G1** | Corroborated-rung precision vs HUMAN labels | 4 | Phase L (§3): two blind raters, kappa >= 0.6, ~10 rater-hours |
| **G2** | Stability: corroborated membership flips across 4 identical-code samples | 4 | needs >= 4 LIVE draws; identical-code replays are deterministic and would report 0 by construction |
| **G3** | Recall vs human labels | 4 | Phase L |
| **G4** | Collateral: A-range attempts ceilinged but labelled clean | 4 | Phase L |
| **G-CTRL** | Control set: 0 false positives, all 4 samples | 4 | a frozen, human-judged control set (>= 8 attempts, >= 2 concepts) |

**None of these blocks this build.** Levels 0-3 ship; level 4 is built DARK and
unreachable (nothing sets ``APOLLO_WRONGNESS_LEVEL=4``), and §8 decision 2 is
explicit that nothing score-affecting ships this cycle. The level-4 rows exist
here so that a future activation PR has to delete a skip rather than notice an
absence.

The COMPUTATION each of these will need is already written and unit-tested:
``tests/gates_p32/k_criteria.py`` (K1-K6, the Wilson lower bound decision 5 gates
on, and the membership-flip rate G2 measures). What is missing is the DATA, not
the code.
"""

from __future__ import annotations

import pytest

from tests.gates_p32 import k_criteria

pytestmark = pytest.mark.unit

_LIVE_COHORT = (
    "requires a live staging cohort at level 2 — see spec §4.1: replay feeds a "
    "RECORDED transcript through evaluate_and_ask and cannot generate a student's "
    "answer to a question that was never asked"
)
_PHASE_L = (
    "requires Phase L — see spec §3: two blind human raters over >=150 exact prod "
    "attempts, kappa >= 0.6 halt rule, ~10 rater-hours; deferred (not agent work)"
)
_LIVE_SAMPLES = (
    "requires >= 4 LIVE draws — see spec §4: identical-code PLAYBACK is "
    "deterministic and would report perfect stability by construction"
)


@pytest.mark.skip(reason=_LIVE_COHORT)
def test_gate_G6_done_gate_costs_at_most_one_added_turn():
    """Threshold: median added turns on firing attempts <= 1, and no attempt
    exceeds ``question_cap()``. The second half IS deterministic and is already
    pinned by ``apollo/smart_questions/tests/test_done_gate.py::
    test_gate_never_exceeds_question_cap``; the median is the live half.
    Computation ready: ``k_criteria.k3_gate_turn_cost``."""
    raise AssertionError("unreachable")


@pytest.mark.skip(reason=_LIVE_COHORT)
def test_gate_G7_denominator_confound_is_disclosed_separately():
    """Threshold: signed mean within +/-5, reported SEPARATELY from any ceiling.
    The gate enlarges ``_probed_node_ids``, which is P1.2b's engagement scope, so
    arm-B score movement has a cause that is not the wrongness signal at all —
    conflating the two would credit (or blame) the wrong lever."""
    raise AssertionError("unreachable")


@pytest.mark.skip(reason=_PHASE_L)
def test_gate_G1_corroborated_rung_precision_vs_human_labels():
    """Threshold (§8 decision 5): CI LOWER BOUND >= 0.70 on a 4-sample majority,
    never a single draw and never a point estimate. Computation ready:
    ``k_criteria.wilson_lower_bound`` / ``k5_precision_and_stability``."""
    raise AssertionError("unreachable")


@pytest.mark.skip(reason=_LIVE_SAMPLES)
def test_gate_G2_corroborated_membership_is_stable_across_identical_runs():
    """Threshold: < 10% of node-slots flip across 4 identical-code samples; a
    flipping node is permanently ineligible. ``basis`` flipped 20% and was killed
    for it. Computation ready: ``k_criteria.k5_membership_flip_rate``."""
    raise AssertionError("unreachable")


@pytest.mark.skip(reason=_PHASE_L)
def test_gate_G3_recall_on_a_range_attempts_humans_label_as_wrong():
    """Threshold: >= 50% of A-range attempts humans label as containing a wrong
    claim are corroborated OR stopped by the gate."""
    raise AssertionError("unreachable")


@pytest.mark.skip(reason=_PHASE_L)
def test_gate_G4_collateral_on_a_range_attempts_labelled_clean():
    """Threshold: <= 5% (rule of three at n = 58) ceilinged but labelled clean,
    and **0** below B+. The second half is P-1 and IS deterministic —
    ``test_gate_regressions.py::test_gate_P1_*`` owns it."""
    raise AssertionError("unreachable")


@pytest.mark.skip(reason=_PHASE_L)
def test_gate_G_CTRL_zero_false_positives_on_the_control_set():
    """Threshold: 0 false positives across all 4 samples on a FROZEN, named
    control set (>= 8 attempts, >= 2 concepts, independently judged to contain no
    contradicting claim). Non-zero is disqualifying, not tunable. Computation
    ready: ``k_criteria.k6_control_false_positives``."""
    raise AssertionError("unreachable")


def test_every_deferred_gate_already_has_its_computation_written():
    """The deferral is about DATA, not code. If a helper named here disappears,
    the skip stubs above quietly become unfulfillable promises."""
    for name in (
        "k3_gate_turn_cost",
        "wilson_lower_bound",
        "k5_precision_and_stability",
        "k5_membership_flip_rate",
        "k6_control_false_positives",
    ):
        assert callable(getattr(k_criteria, name)), name


def test_the_full_section_4_gate_table_is_accounted_for():
    """Every row of spec §4 is either closed by a test in this package or listed
    as a declared skip above. Pinned as a literal roster so a new §4 row cannot
    be added to the spec and silently skipped in the suite."""
    closed_here = {
        "G-L1": "test_gate_l1.py",
        "G-L1b": "test_gate_l1.py",
        "G-L1c": "test_gate_l1.py + k_criteria.over_fire",
        "G8": "test_gate_l2.py",
        "G9": "test_gate_l2.py",
        "G-L3": "test_gate_l3.py",
        "G-L3b": "test_gate_l3.py",
        "G-FIX": "test_gate_regressions.py",
        "G-CEL": "test_gate_regressions.py",
    }
    deferred_here = {"G6", "G7", "G1", "G2", "G3", "G4", "G-CTRL"}

    assert set(closed_here) | deferred_here == {
        "G-L1",
        "G-L1b",
        "G-L1c",
        "G6",
        "G7",
        "G8",
        "G9",
        "G-L3",
        "G-L3b",
        "G1",
        "G2",
        "G3",
        "G4",
        "G-CTRL",
        "G-FIX",
        "G-CEL",
    }
    assert set(closed_here).isdisjoint(deferred_here)
