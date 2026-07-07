"""Sanity checks for the Resolver V2 view-cache generator + committed artifact (T2).

Two layers, both offline (no OpenAI, no network):

1. Unit: the affirmative-view gate (``view_offenses`` / ``clean_views``)
   rejects negation, litotes, hedges, and over-long views, and dedups against
   the label (the loader prepends the label as view 0).
2. Artifact: the committed ``apollo/resolver_v2/views/views_cache.json`` parses,
   covers every reference node of every enumerated problem payload with a
   non-empty view list, and every view passes the gate (card-T2 DONE gate).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The generator lives in scripts/ (a non-package dir). Put it on sys.path so it
# is importable by name regardless of the pytest rootdir / import mode.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import generate_resolver_v2_views as gen  # noqa: E402  (path-dependent import)


# ---------------------------------------------------------------------------
# 1. The affirmative-view gate
# ---------------------------------------------------------------------------


def test_view_offenses_accepts_clean_affirmative_view():
    assert gen.view_offenses("The flow speed increases when the pipe narrows.") == ()


def test_view_offenses_rejects_negation_litotes_hedge_long_empty():
    assert any(o.startswith("negation") for o in gen.view_offenses("Pressure does not change."))
    assert any(o.startswith("negation") for o in gen.view_offenses("The height never varies."))
    assert any(o.startswith("negation") for o in gen.view_offenses("It doesn't compress."))
    # Litotes ("no change") is ALSO rejected here — stricter than the polarity
    # runtime screen by design (§5.2: no litotes in generated views).
    assert any(o.startswith("negation") for o in gen.view_offenses("There is no change in height."))
    assert any(o.startswith("hedge") for o in gen.view_offenses("The pressure might drop."))
    long_view = " ".join(["word"] * (gen.MAX_VIEW_WORDS + 1))
    assert any(o.startswith("too_long") for o in gen.view_offenses(long_view))
    assert gen.view_offenses("   ") == ("empty",)


def test_clean_views_dedups_label_and_caps():
    label = "Continuity (mass conservation)"
    candidates = [
        "continuity (mass conservation)",  # dup of label -> silently dropped
        "Mass flow rate stays the same along the pipe.",
        "Mass flow rate stays the same along the pipe.",  # dup of prior view
        "The pipe cannot compress the fluid.",  # negation -> rejected
        "Area times velocity is constant.",
        "The fluid conserves mass as it flows.",
        "Flow into a section equals flow out of it.",
        "One extra view beyond the cap.",
    ]
    kept, rejected = gen.clean_views(candidates, label=label, keep=[])
    assert len(kept) == gen.MAX_VIEWS_PER_KEY
    assert "Mass flow rate stays the same along the pipe." in kept
    assert all("cannot" not in v for v in kept)
    assert any("negation" in ",".join(off) for _, off in rejected)


# ---------------------------------------------------------------------------
# 2. The committed artifact (card-T2 DONE gate)
# ---------------------------------------------------------------------------


def test_committed_views_cache_passes_card_gate():
    assert gen.CACHE_PATH.exists(), f"views cache missing at {gen.CACHE_PATH}"
    cache = gen.load_cache()
    problems = [gen.read_problem(p) for p in gen.enumerate_problem_files([])]
    assert problems, "no problem payloads enumerated"
    errors, rows = gen.validate_cache(cache, problems)
    assert errors == [], "artifact gate failed:\n" + "\n".join(errors)
    assert len(rows) == len(problems)
    # The F1c corpus problem set must be covered (16 fluid + 18 macro + 4
    # linear_motion personas map onto these 11 problems).
    expected_pairs = {
        "bernoulli_principle/bernoulli_horizontal_pipe_find_p2",
        "bernoulli_principle/bernoulli_height_change_find_v2",
        "bernoulli_principle/bernoulli_full_find_p2",
        "continuity_equation/continuity_area_change_find_v2",
        "volumetric_flow_rate/volumetric_flow_rate_find_Q",
        "gdp_components/gdp_identity",
        "gdp_components/net_exports_sign",
        "gdp_components/nnp_chain",
        "nominal_vs_real_gdp/real_gdp_from_deflator",
        "nominal_vs_real_gdp/real_gdp_growth",
        "kinematics_constant_acceleration/cyclist_accel_v_and_distance",
    }
    assert expected_pairs <= set(cache.keys())


def test_committed_views_contain_no_card_gate_markers():
    """The card's literal artifact gate: no view contains ' not ' / 'never' / n't."""
    cache = gen.load_cache()
    for pair, entry in cache.items():
        if pair == "_meta":
            continue
        for key, views in entry.items():
            for view in views:
                low = f" {view.lower()} "
                assert " not " not in low, f"{pair}::{key}: {view!r}"
                assert "never" not in low, f"{pair}::{key}: {view!r}"
                assert "n't" not in low, f"{pair}::{key}: {view!r}"
