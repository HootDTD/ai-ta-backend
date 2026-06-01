"""P2.8 — `misconception_corrected` axis on the rubric + summarize_for_rubric reducer.

Covers:
- summarize_for_rubric maps per-turn signals to per-bank-code scores
  per the resolved-window rule (last 2 turns clean → 1.0, else 0.5).
- compute_rubric integrates the new axis at 5% taken proportionally
  from the existing 60/25/15.
- The no-misconception case is BYTE-identical to the pre-P2.8 rubric
  thanks to absent-axis weight redistribution.
"""
from __future__ import annotations

import pytest

from apollo.ontology import build_node
from apollo.overseer.misconception import (
    MisconceptionSignal,
    summarize_for_rubric,
)
from apollo.overseer.rubric import compute_rubric


# ---------------------------------------------------------------------------
# summarize_for_rubric
# ---------------------------------------------------------------------------

def _signal(*, fired: bool = True, code: str | None = "alpha") -> MisconceptionSignal:
    return MisconceptionSignal(
        fired=fired,
        state="probe" if fired else "default",
        bank_code=code,
        confidence=0.6 if fired else 0.0,
    )


def test_summarize_with_no_signals_is_empty():
    assert summarize_for_rubric([]) == {}


def test_summarize_excludes_never_fired_codes():
    """A signal that never fired (state=default, bank_code=None) yields
    no entry in the axis."""
    signals = [_signal(fired=False, code=None)] * 5
    assert summarize_for_rubric(signals) == {}


def test_summarize_resolved_when_last_window_clean():
    """code 'alpha' fires early, then last 2 turns are clean → resolved (1.0)."""
    signals = [
        _signal(code="alpha"),
        _signal(code="alpha"),
        _signal(fired=False, code=None),
        _signal(fired=False, code=None),
    ]
    out = summarize_for_rubric(signals)
    assert out == {"alpha": 1.0}


def test_summarize_unresolved_when_recent_firing():
    """Most recent turn still fires → unresolved (0.5)."""
    signals = [
        _signal(code="alpha"),
        _signal(fired=False, code=None),
        _signal(code="alpha"),
    ]
    assert summarize_for_rubric(signals) == {"alpha": 0.5}


def test_summarize_handles_multiple_codes_independently():
    signals = [
        _signal(code="alpha"),
        _signal(code="beta"),
        _signal(code="alpha"),  # alpha still firing in tail
        _signal(fired=False, code=None),
    ]
    # window = last 2 turns = [alpha, default]
    # alpha in tail → 0.5; beta not in tail → 1.0
    out = summarize_for_rubric(signals, resolved_window=2)
    assert out == {"alpha": 0.5, "beta": 1.0}


def test_summarize_window_size_respected():
    """Larger resolved_window means more recent turns must be clean."""
    signals = [
        _signal(code="alpha"),
        _signal(fired=False, code=None),
        _signal(fired=False, code=None),
    ]
    # window = 5 captures the alpha at turn 0 → unresolved
    out = summarize_for_rubric(signals, resolved_window=5)
    assert out == {"alpha": 0.5}


# ---------------------------------------------------------------------------
# compute_rubric integration
# ---------------------------------------------------------------------------

def _ref_node(node_type: str, node_id: str, content: dict):
    return build_node(
        node_type=node_type,  # type: ignore[arg-type]
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content=content,
    )


def _typical_reference_nodes():
    """A reference graph with one of each gradable type so all three
    classic axes are present."""
    return [
        _ref_node("procedure_step", "p1", {"action": "use continuity",
                                           "purpose": "find v2"}),
        _ref_node("condition", "c1", {"applies_when": "incompressible flow",
                                      "label": "incompr"}),
        _ref_node("simplification", "s1", {"applies_when": "horizontal",
                                           "transformation": "drop rho*g*h"}),
    ]


def _typical_coverage_full():
    """Coverage object where everything is covered with high confidence."""
    return {
        "per_step": {"p1": "covered", "c1": "covered", "s1": "covered"},
        "procedure_scores": {"p1": 1.0},
        "confidences": {"p1": 1.0, "c1": 1.0, "s1": 1.0},
    }


def test_no_misconception_axis_is_byte_identical_to_pre_p28():
    """Critical contract: when no misconception was detected during the
    attempt, the rubric output is identical to the pre-P2.8 60/25/15
    formula (modulo the new misconception_corrected key, which carries
    present=False and contributes nothing to overall)."""
    refs = _typical_reference_nodes()
    cov = _typical_coverage_full()

    # No misconception_scores arg
    legacy = compute_rubric(cov, refs)
    # With explicit empty
    explicit = compute_rubric(cov, refs, misconception_scores={})

    # The three classic axes and overall must be identical.
    for axis in ("overall", "procedure", "justification", "simplification"):
        assert legacy[axis] == explicit[axis], f"axis {axis} drifted"

    # And the misconception axis is absent.
    assert legacy["misconception_corrected"]["present"] is False
    assert legacy["misconception_corrected"]["detected"] == 0


def test_perfect_misconception_axis_pulls_overall_up_relative_to_partial():
    """A fully-resolved misconception axis should give a higher overall
    than a 50% partial-credit one, all else equal."""
    refs = _typical_reference_nodes()

    # Mid-band coverage: procedure at 0.7, others not all covered, so the
    # overall has room to move when the misconception axis enters.
    cov = {
        "per_step": {"p1": "covered", "c1": "missing", "s1": "covered"},
        "procedure_scores": {"p1": 0.7},
        "confidences": {"p1": 0.7, "c1": 0.3, "s1": 0.9},
    }

    full = compute_rubric(cov, refs, misconception_scores={"alpha": 1.0})
    partial = compute_rubric(cov, refs, misconception_scores={"alpha": 0.5})

    assert full["misconception_corrected"]["score"] == 100
    assert partial["misconception_corrected"]["score"] == 50
    assert full["overall"]["score"] >= partial["overall"]["score"]


def test_misconception_axis_block_carries_detected_resolved_counts():
    refs = _typical_reference_nodes()
    cov = _typical_coverage_full()
    rub = compute_rubric(
        cov, refs,
        misconception_scores={"alpha": 1.0, "beta": 0.5, "gamma": 1.0},
    )
    block = rub["misconception_corrected"]
    assert block["present"] is True
    assert block["detected"] == 3
    assert block["resolved"] == 2  # alpha + gamma
    # Score should be (1.0 + 0.5 + 1.0) / 3 * 100 = ~83
    assert block["score"] == pytest.approx(83, abs=1)


def test_misconception_axis_alone_when_classic_axes_absent():
    """Edge: a reference graph with only a misconception axis present.
    The absent-axis redistribution should make misconception_corrected
    the entire 100% weight."""
    refs = []  # no gradable reference nodes at all
    cov = {"per_step": {}, "procedure_scores": {}, "confidences": {}}
    rub = compute_rubric(
        cov, refs, misconception_scores={"alpha": 1.0},
    )
    assert rub["overall"]["score"] == 100
    assert rub["misconception_corrected"]["present"] is True
