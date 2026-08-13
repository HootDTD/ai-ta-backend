"""The K-criteria DRY RUN — exercise the kill switches before they are needed.

Spec §2.6 lists six kill criteria ("any one -> decrement and stop"). Plan
§WAVE 4 assigns agent C1 a "K-criteria dry run: compute what K1-K6 would evaluate
to on arm L1's output, so the kill criteria are exercised before they are ever
needed live". ``tests/gates_p32/k_criteria.py`` is that computation; this module
is its unit test, on SYNTHETIC rows.

Synthetic on purpose: the point of a dry run is that every criterion is shown
able to FIRE and able to PASS. A campaign corpus can only ever demonstrate one of
those per criterion, and a gate that has never been seen to fire is not a gate.

The parser is additionally pinned against the REAL log lines a real
``handle_done`` emits — that assertion lives in ``test_gate_l1.py`` (G-L1c), so
the two halves cannot agree with a shared fiction.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.gates_p32 import k_criteria as kc

pytestmark = pytest.mark.unit


def _summary(**overrides: Any) -> kc.ShadowSummary:
    values: dict[str, Any] = {
        "attempt_id": "1",
        "findings": 1,
        "nodes": 1,
        "ledger_entries": 100,
        "corroborated": 1,
        "would_ceiling": 0,
        "level": 1,
    }
    values.update(overrides)
    return kc.ShadowSummary(**values)


# --------------------------------------------------------------------------- #
# Log parsing                                                                  #
# --------------------------------------------------------------------------- #

_OBSERVED = (
    "apollo_wrongness_observed attempt_id=99 node_id=eq1 rung=corroborated "
    "span_verified=True second_reader={'contradicted': True, 'corrected_later': False, "
    "'prompted': True} would_ceiling=True kind=reversal"
)
_SUMMARY = (
    "apollo_wrongness_summary attempt_id=99 findings=1 nodes=1 ledger_entries=2 "
    "corroborated=1 would_ceiling=1 level=1"
)


def test_the_observed_parser_survives_the_embedded_dict():
    """``second_reader=`` renders a dict and therefore CONTAINS SPACES. A generic
    ``key=value`` whitespace split silently mis-reads every field after it — the
    exact shape of bug that would make the whole corpus unreadable."""
    (observation,) = kc.parse_shadow_observations([_OBSERVED])

    assert observation.attempt_id == "99"
    assert observation.node_id == "eq1"
    assert observation.rung == "corroborated"
    assert observation.span_verified is True
    assert observation.would_ceiling is True
    assert observation.kind == "reversal"


def test_the_summary_parser_reads_the_over_fire_denominator():
    (summary,) = kc.parse_shadow_summaries([_SUMMARY])

    assert (summary.findings, summary.ledger_entries) == (1, 2)
    assert summary.corroborated == 1
    assert summary.level == 1


def test_unrelated_and_malformed_lines_are_skipped_not_raised():
    lines = ["some other log line", "apollo_wrongness_summary attempt_id=7", _SUMMARY]

    assert len(kc.parse_shadow_summaries(lines)) == 2
    assert kc.parse_shadow_summaries(lines)[0].findings == 0  # absent -> 0, never a crash
    assert kc.parse_shadow_observations(lines) == ()


# --------------------------------------------------------------------------- #
# G-L1c                                                                        #
# --------------------------------------------------------------------------- #


def test_over_fire_rate_is_a_corpus_ratio_not_a_per_attempt_one():
    """Summed numerator over summed denominator: an attempt with a 40-entry
    ledger and one label must not weigh the same as one with two entries."""
    rate = kc.over_fire(
        [_summary(findings=1, ledger_entries=40), _summary(findings=1, ledger_entries=60)]
    )

    assert (rate.tagged, rate.ledger_entries) == (2, 100)
    assert rate.rate == pytest.approx(0.02)
    assert rate.within_threshold is True


def test_over_fire_fires_at_the_threshold_and_is_honest_about_an_empty_corpus():
    assert kc.over_fire([_summary(findings=10, ledger_entries=100)]).within_threshold is False
    assert kc.over_fire([_summary(findings=9, ledger_entries=100)]).within_threshold is True

    empty = kc.over_fire([])
    assert empty.computable is False
    assert empty.within_threshold is False, "an unmeasurable corpus is not a passing one"


# --------------------------------------------------------------------------- #
# K1 - K6                                                                      #
# --------------------------------------------------------------------------- #


def test_K1_fires_on_both_ends_of_the_band():
    too_many = [_summary(attempt_id=str(i), would_ceiling=1) for i in range(10)]
    too_few = [_summary(attempt_id=str(i)) for i in range(100)]
    goldilocks = [_summary(attempt_id=str(i), would_ceiling=1 if i < 10 else 0) for i in range(100)]

    assert kc.k1_ceiling_fire_rate(too_many).triggered is True
    assert kc.k1_ceiling_fire_rate(too_few).triggered is True
    assert kc.k1_ceiling_fire_rate(goldilocks).triggered is False
    assert kc.k1_ceiling_fire_rate([]).computable is False


def test_K2_treats_a_single_d_or_f_as_a_design_defect():
    """P-1 makes this impossible, so one occurrence is not a tuning problem."""
    assert kc.k2_ceilinged_letters(["A", "B+", "B"]).triggered is False
    assert kc.k2_ceilinged_letters(["A", "D"]).triggered is True
    assert kc.k2_ceilinged_letters(["F"]).triggered is True
    assert kc.k2_ceilinged_letters([]).computable is False


def test_K3_measures_medians_on_firing_attempts_only():
    assert kc.k3_gate_turn_cost(baseline_turns=[4, 5, 6], gated_turns=[5, 6, 7]).triggered is False
    assert kc.k3_gate_turn_cost(baseline_turns=[4, 4, 4], gated_turns=[8, 8, 8]).triggered is True
    assert kc.k3_gate_turn_cost(baseline_turns=[], gated_turns=[]).computable is False


def test_K3_also_fires_on_the_auto_done_latency_arm():
    assert (
        kc.k3_gate_turn_cost(
            baseline_turns=[4],
            gated_turns=[4],
            baseline_latency_p50=10.0,
            gated_latency_p50=13.0,
        ).triggered
        is True
    )
    assert (
        kc.k3_gate_turn_cost(
            baseline_turns=[4],
            gated_turns=[4],
            baseline_latency_p50=10.0,
            gated_latency_p50=11.0,
        ).triggered
        is False
    )


def test_K4_is_a_hair_trigger_and_is_a_human_input():
    """Not derivable from any corpus — a human confirms a pilot complaint that a
    narrative accused a student falsely. R9 makes level 3 student-visible, so
    this is the surface that criterion watches."""
    assert kc.k4_false_accusation_reports(0).triggered is False
    assert kc.k4_false_accusation_reports(1).triggered is True


def test_K5_gates_on_the_ci_lower_bound_because_a_point_estimate_cannot_fail():
    """Decision 5's arithmetic: the exact binomial 95% CI on 4/4 is [40%, 100%],
    so "precision >= 0.85" is unfalsifiable at that size. The Wilson lower bound
    on 4/4 is well under the 0.70 bar, which is the whole point."""
    assert kc.wilson_lower_bound(4, 4) < kc.K5_MIN_PRECISION_CI_LOWER
    assert kc.wilson_lower_bound(40, 40) > kc.K5_MIN_PRECISION_CI_LOWER
    assert kc.wilson_lower_bound(0, 0) == 0.0

    thin = kc.k5_precision_and_stability(true_positives=4, labelled_findings=4, samples=[{"a"}] * 4)
    assert thin.triggered is True

    solid = kc.k5_precision_and_stability(
        true_positives=40, labelled_findings=40, samples=[{"a"}] * 4
    )
    assert solid.triggered is False


def test_K5_membership_flips_are_measured_per_node_slot():
    """ "A flipping node is permanently ineligible" — ``basis`` flipped 20% and
    was killed for it."""
    stable = [{"a", "b"}, {"a", "b"}, {"a", "b"}, {"a", "b"}]
    flappy = [{"a", "b"}, {"a"}, {"a", "b"}, {"a"}]

    assert kc.k5_membership_flip_rate(stable) == 0.0
    assert kc.k5_membership_flip_rate(flappy) == pytest.approx(0.5)
    assert kc.k5_membership_flip_rate([]) == 0.0

    assert (
        kc.k5_precision_and_stability(
            true_positives=40, labelled_findings=40, samples=flappy
        ).triggered
        is True
    )


def test_K5_is_not_computable_below_the_live_sample_minimum():
    """House rule: no live arm is concluded from fewer than four draws."""
    verdict = kc.k5_precision_and_stability(
        true_positives=40, labelled_findings=40, samples=[{"a"}, {"a"}]
    )

    assert verdict.computable is False
    assert verdict.triggered is False


def test_K6_treats_any_control_false_positive_as_disqualifying():
    """Control FP stayed 0/4 across every detector run ever measured; non-zero is
    disqualifying, not tunable."""
    assert kc.k6_control_false_positives({"a77": 0, "a89": 0, "a97": 0}).triggered is False
    assert kc.k6_control_false_positives({"a77": 0, "a89": 1}).triggered is True
    assert kc.k6_control_false_positives({}).computable is False


# --------------------------------------------------------------------------- #
# The assembled table                                                          #
# --------------------------------------------------------------------------- #


def test_the_dry_run_produces_all_six_in_order_with_measured_values():
    verdicts = kc.evaluate_k_criteria(
        summaries=[
            _summary(attempt_id=str(i), would_ceiling=1 if i < 10 else 0) for i in range(100)
        ],
        ceilinged_letters=["A", "B+"],
        baseline_turns=[4, 5],
        gated_turns=[5, 6],
        verified_complaints=0,
        true_positives=40,
        labelled_findings=40,
        corroborated_samples=[{"a"}] * 4,
        control_findings={"a77": 0, "a89": 0},
    )

    assert [verdict.criterion for verdict in verdicts] == ["K1", "K2", "K3", "K4", "K5", "K6"]
    assert kc.any_triggered(verdicts) is False
    assert all(verdict.measured and verdict.threshold for verdict in verdicts)


def test_any_triggered_is_the_stop_rule():
    """Spec §2.6: "any one -> decrement and stop". A single fire is enough."""
    verdicts = kc.evaluate_k_criteria(
        summaries=[
            _summary(attempt_id=str(i), would_ceiling=1 if i < 10 else 0) for i in range(100)
        ],
        ceilinged_letters=["A", "F"],
        verified_complaints=0,
        control_findings={"a77": 0},
    )

    assert kc.any_triggered(verdicts) is True
    assert [verdict.criterion for verdict in verdicts if verdict.triggered] == ["K2"]


def test_an_uncomputable_criterion_is_never_silently_a_pass():
    """ "We could not measure it" is not "it passed" — the campaign report must
    print ``computable=False`` beside the threshold rather than a green tick."""
    verdicts = kc.evaluate_k_criteria(summaries=[])
    by_id = {verdict.criterion: verdict for verdict in verdicts}

    assert by_id["K1"].computable is False
    assert by_id["K2"].computable is False
    assert by_id["K5"].computable is False
    assert by_id["K6"].computable is False
    assert kc.any_triggered(verdicts) is False  # ...and none of them CLAIMS a failure either
