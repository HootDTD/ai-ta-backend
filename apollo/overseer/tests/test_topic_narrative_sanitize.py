"""2026-07-11 feedback spec §2 — deterministic internals gate on the narrative.

The gate is the belt-and-suspenders layer under the prompt fix: even if the
LLM leaks ledger internals (canonical keys, credit/weight decimals, dock
values), the served narrative must not contain them. Percentages the topic
list already shows are allowed and must survive.
"""

from __future__ import annotations

import pytest

from apollo.overseer.topic_narrative import sanitize_narrative

pytestmark = pytest.mark.unit

# The exact leak observed live on staging (attempt 62, MGMT course).
_LEAKY = (
    "You clearly explained the directional relationship between upstream and "
    "downstream as movement from source to destination "
    "(proc_explain_directionality, credit 0.80, weight 0.77). You also "
    "successfully described why this matters "
    "(proc_explain_causality, credit 0.90, weight 0.23).\n\n"
    "No points were docked for errors (misconception dock: 0.000)."
)
_KEYS = ["proc_explain_directionality", "proc_explain_causality"]


def test_strips_observed_staging_leak():
    out = sanitize_narrative(_LEAKY, canonical_keys=_KEYS)
    assert "proc_explain_directionality" not in out
    assert "proc_explain_causality" not in out
    assert "credit" not in out.lower()
    assert "weight 0.77" not in out
    # The internal fragment goes; the plain-English word "docked" is fine.
    assert "dock: 0.000" not in out
    assert "0.80" not in out and "0.23" not in out and "0.000" not in out
    # Prose survives, including the sentence that lost its parenthetical.
    assert "directional relationship" in out
    assert "No points were docked for errors" in out


def test_no_empty_parens_or_dangling_punctuation_left_behind():
    out = sanitize_narrative(_LEAKY, canonical_keys=_KEYS)
    assert "()" not in out
    assert "( ," not in out and "(, " not in out
    assert " ." not in out and " ," not in out


def test_percentages_are_preserved():
    text = "You earned 80% on the causality topic and 100% on the definition."
    assert sanitize_narrative(text, canonical_keys=_KEYS) == text


def test_inline_scoring_fragments_without_parens_are_stripped():
    text = "That topic had credit=0.80 and weight: 0.77 overall."
    out = sanitize_narrative(text, canonical_keys=[])
    assert "0.80" not in out and "0.77" not in out
    assert "credit" not in out.lower() and "weight" not in out.lower()


def test_physics_weight_prose_is_not_stripped():
    # "weight" as a physics word (no 0-1 decimal after it) must survive.
    text = "The weight of the fluid column ($w = mg$) pushes down, so weight = mg."
    assert sanitize_narrative(text, canonical_keys=[]) == text


def test_math_spans_preserved():
    text = "Bernoulli: $P + 0.5 \\rho v^2 = const$ along a streamline."
    assert sanitize_narrative(text, canonical_keys=[]) == text


def test_idempotent():
    once = sanitize_narrative(_LEAKY, canonical_keys=_KEYS)
    assert sanitize_narrative(once, canonical_keys=_KEYS) == once


def test_backticked_keys_stripped():
    text = "You missed `def.def_future_shock` here."
    out = sanitize_narrative(text, canonical_keys=["def.def_future_shock"])
    assert "def_future_shock" not in out
    assert "`" not in out


def test_general_bucket_key_and_empty_keys_are_safe():
    text = "Other issues were minor."
    assert sanitize_narrative(text, canonical_keys=["_general", ""]) == text


def test_empty_and_placeholder_text_untouched():
    assert sanitize_narrative("", canonical_keys=[]) == ""
    placeholder = "[Diagnostic narrative unavailable — the grade above is still accurate.]"
    assert sanitize_narrative(placeholder, canonical_keys=[]) == placeholder


def test_out_of_range_decimals_survive():
    text = "The weight 1.5 factor was applied to a weight 2.5 kg mass."
    assert sanitize_narrative(text, canonical_keys=[]) == text


def test_full_credit_scoring_decimals_stripped():
    out = sanitize_narrative("That topic had credit 1.00 and weight 1.0.", canonical_keys=[])
    assert "1.00" not in out and "1.0" not in out
    assert "credit" not in out.lower() and "weight" not in out.lower()


def test_boundary_fragment_leaves_no_stray_whitespace():
    assert sanitize_narrative("Text ends with weight 0.5", canonical_keys=[]) == "Text ends with"
