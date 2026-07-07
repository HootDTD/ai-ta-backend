"""Campaign-plan Task A2 Step 1 — the composite-formula unit tests."""

from __future__ import annotations

from apollo.grading.composite import CompositeWeights, composite_score, load_weights


def test_formula_is_weighted_sum_minus_penalty():
    # 2026-07-07 renormalized defaults: w_n + w_e = 1.0, so a perfect attempt
    # reaches composite 1.0 (was 0.85 == the Strong band cut).
    w = CompositeWeights(w_n=0.706, w_e=0.294, p=0.15)
    assert composite_score(1.0, 1.0, 0.0, w) == 1.0
    assert composite_score(0.5, 0.4, 1.0, w) == round(0.706 * 0.5 + 0.294 * 0.4 - 0.15 * 1.0, 6)


def test_clamped_to_unit_interval():
    w = CompositeWeights(w_n=0.706, w_e=0.294, p=0.15)
    assert composite_score(0.0, 0.0, 1.0, w) == 0.0


def test_clamped_at_upper_bound():
    w = CompositeWeights(w_n=1.0, w_e=1.0, p=0.0)
    assert composite_score(1.0, 1.0, 0.0, w) == 1.0


def test_default_weights_match_spec():
    # Renormalized 2026-07-07 (lane D): w_n + w_e must sum to exactly 1.0.
    w = load_weights()
    assert w == CompositeWeights(w_n=0.706, w_e=0.294, p=0.15)
    assert w.w_n + w.w_e == 1.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("APOLLO_COMPOSITE_W_NODE", "0.7")
    assert load_weights().w_n == 0.7


def test_env_override_all_three(monkeypatch):
    monkeypatch.setenv("APOLLO_COMPOSITE_W_NODE", "0.5")
    monkeypatch.setenv("APOLLO_COMPOSITE_W_EDGE", "0.3")
    monkeypatch.setenv("APOLLO_COMPOSITE_P_MISC", "0.2")
    w = load_weights()
    assert w == CompositeWeights(w_n=0.5, w_e=0.3, p=0.2)


def test_env_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APOLLO_COMPOSITE_W_NODE", "not-a-float")
    assert load_weights().w_n == 0.706


def test_weights_are_frozen():
    w = CompositeWeights(w_n=0.1, w_e=0.2, p=0.3)
    try:
        w.w_n = 0.9  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - defensive, dataclass(frozen=True) always raises
        raise AssertionError("CompositeWeights must be frozen")
