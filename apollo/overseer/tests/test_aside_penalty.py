"""Pure-function tests for the INTERACTION5 Hoot-assist grading cap.

Covers ``apollo.overseer.aside_penalty.apply_aside_caps`` end to end AND the two
downstream consumers it must keep in agreement (``topic_score._credit_for_node``
and ``rubric.compute_rubric``), plus the ``config.settings.interaction5_enabled``
flag getter.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from apollo.overseer.aside_penalty import apply_aside_caps
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.topic_score import _credit_for_node
from config.settings import interaction5_enabled

pytestmark = pytest.mark.unit


def _coverage(**overrides) -> dict:
    base = {
        "per_step": {},
        "procedure_scores": {},
        "confidences": {},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Cap math + both-lane agreement
# --------------------------------------------------------------------------- #
def test_covered_procedure_node_caps_to_partial_in_both_lanes():
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": 1.0},
        confidences={"eq1": 0.9},
        hoot_assisted={"eq1": True},
    )

    capped, ids = apply_aside_caps(coverage)

    assert ids == ("eq1",)
    # Topic lane: credit lowered to the cap, status downgraded to partial.
    assert _credit_for_node("eq1", capped) == (0.5, "partial")
    # Rubric lane: procedure axis reads the capped 0.5 -> 50, never the full 100.
    from apollo.ontology import build_node

    node = build_node(
        node_type="procedure_step",
        node_id="eq1",
        attempt_id=1,
        source="reference",
        content={"action": "eq1"},
    )
    rubric = compute_rubric(capped, [node])
    assert rubric["procedure"]["score"] == 50


def test_binary_covered_node_without_procedure_score_lands_at_cap_partial():
    """A binary covered node whose effective credit is the implicit 1.0 (present
    in per_step, absent from procedure_scores) must land at 0.5/partial, not
    0.0/missing — mirrors ``_credit_for_node``'s ``(1.0, "covered")`` branch."""
    coverage = _coverage(
        per_step={"c1": "covered"},
        procedure_scores={},
        hoot_assisted={"c1": True},
    )

    capped, ids = apply_aside_caps(coverage)

    assert ids == ("c1",)
    assert _credit_for_node("c1", capped) == (0.5, "partial")
    # Rubric binary axis: forced below covered (0/1 axis has no partial).
    from apollo.ontology import build_node

    cond = build_node(
        node_type="condition",
        node_id="c1",
        attempt_id=1,
        source="reference",
        content={"applies_when": "steady flow", "label": "steady"},
    )
    rubric = compute_rubric(capped, [cond])
    assert rubric["justification"]["score"] == 0


def test_capped_zero_credit_node_lands_missing():
    coverage = _coverage(
        per_step={"eq1": "missing"},
        procedure_scores={"eq1": 0.0},
        hoot_assisted={"eq1": True},
    )

    capped, ids = apply_aside_caps(coverage)

    assert ids == ("eq1",)
    assert _credit_for_node("eq1", capped) == (0.0, "missing")


def test_credit_below_cap_is_left_at_its_lower_value():
    """min(earned, cap): an assisted node already below the cap keeps its credit,
    only its status is forced off ``covered``."""
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": 0.3},
        hoot_assisted={"eq1": True},
    )

    capped, _ids = apply_aside_caps(coverage)

    assert _credit_for_node("eq1", capped) == (0.3, "partial")


def test_custom_cap_is_honored():
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": 1.0},
        hoot_assisted={"eq1": True},
    )

    capped, _ids = apply_aside_caps(coverage, cap=0.25)

    assert _credit_for_node("eq1", capped) == (0.25, "partial")


# --------------------------------------------------------------------------- #
# Unassisted nodes untouched; assisted ids sorted
# --------------------------------------------------------------------------- #
def test_unassisted_nodes_are_byte_identical_and_ids_sorted():
    coverage = _coverage(
        per_step={"a": "covered", "z": "covered", "m": "covered"},
        procedure_scores={"a": 1.0, "z": 1.0, "m": 0.8},
        hoot_assisted={"z": True, "a": True, "m": False},
    )

    capped, ids = apply_aside_caps(coverage)

    # Sorted assisted ids only.
    assert ids == ("a", "z")
    # Unassisted node m carried through unchanged in BOTH maps.
    assert capped["procedure_scores"]["m"] == 0.8
    assert capped["per_step"]["m"] == "covered"
    assert _credit_for_node("m", capped) == (0.8, "covered")
    # Assisted nodes capped + downgraded.
    assert _credit_for_node("a", capped) == (0.5, "partial")
    assert _credit_for_node("z", capped) == (0.5, "partial")


# --------------------------------------------------------------------------- #
# No mutation of the input
# --------------------------------------------------------------------------- #
def test_input_is_never_mutated():
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": 1.0},
        confidences={"eq1": 0.9},
        hoot_assisted={"eq1": True},
    )
    snapshot = deepcopy(coverage)

    capped, _ids = apply_aside_caps(coverage)

    # Original object and its inner maps are unchanged.
    assert coverage == snapshot
    assert coverage["per_step"] is not capped["per_step"]
    assert coverage["procedure_scores"] is not capped["procedure_scores"]
    # Untouched maps are carried by reference (cheap, and never mutated).
    assert capped["confidences"] is coverage["confidences"]
    assert capped["hoot_assisted"] is coverage["hoot_assisted"]


# --------------------------------------------------------------------------- #
# Absent / empty / all-False flag -> same object, empty ids
# --------------------------------------------------------------------------- #
def test_absent_flag_returns_input_unchanged():
    coverage = _coverage(per_step={"eq1": "covered"}, procedure_scores={"eq1": 1.0})

    result, ids = apply_aside_caps(coverage)

    assert result is coverage
    assert ids == ()


def test_empty_flag_map_returns_input_unchanged():
    coverage = _coverage(
        per_step={"eq1": "covered"}, procedure_scores={"eq1": 1.0}, hoot_assisted={}
    )

    result, ids = apply_aside_caps(coverage)

    assert result is coverage
    assert ids == ()


def test_all_false_flag_map_returns_input_unchanged():
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": 1.0},
        hoot_assisted={"eq1": False, "eq2": False},
    )

    result, ids = apply_aside_caps(coverage)

    assert result is coverage
    assert ids == ()


def test_nonfinite_effective_credit_coerces_to_zero_then_caps():
    """A malformed procedure score coerces to 0.0 (matching the lanes'
    ``_finite_score``) and min(0.0, cap) == 0.0 -> missing."""
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": float("nan")},
        hoot_assisted={"eq1": True},
    )

    capped, _ids = apply_aside_caps(coverage)

    assert _credit_for_node("eq1", capped) == (0.0, "missing")


@pytest.mark.parametrize("bad", [None, "not-a-number"])
def test_non_numeric_procedure_score_coerces_to_zero(bad):
    """A non-numeric procedure score (``float`` raises Type/ValueError) coerces to
    0.0 -> min(0.0, cap) == 0.0 -> missing (defensive; the contract validates
    numbers, but the cap must never crash grading)."""
    coverage = _coverage(
        per_step={"eq1": "covered"},
        procedure_scores={"eq1": bad},
        hoot_assisted={"eq1": True},
    )

    capped, _ids = apply_aside_caps(coverage)

    assert _credit_for_node("eq1", capped) == (0.0, "missing")


def test_assisted_node_absent_from_procedure_scores_and_not_covered_is_zero():
    """A node the assist map flags but that carries neither a procedure score nor
    a ``"covered"`` per_step has effective credit 0.0 -> capped to 0.0/missing."""
    coverage = _coverage(
        per_step={"eq1": "missing"},
        procedure_scores={},
        hoot_assisted={"eq1": True},
    )

    capped, ids = apply_aside_caps(coverage)

    assert ids == ("eq1",)
    assert capped["procedure_scores"]["eq1"] == 0.0
    assert _credit_for_node("eq1", capped) == (0.0, "missing")


# --------------------------------------------------------------------------- #
# INTERACTION5 flag getter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["1", "true", "on", "yes", "TRUE", "Yes"])
def test_interaction5_enabled_truthy(monkeypatch, raw):
    monkeypatch.setenv("INTERACTION5", raw)
    assert interaction5_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "FALSE"])
def test_interaction5_enabled_falsy(monkeypatch, raw):
    monkeypatch.setenv("INTERACTION5", raw)
    assert interaction5_enabled() is False


def test_interaction5_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("INTERACTION5", raising=False)
    assert interaction5_enabled() is False
