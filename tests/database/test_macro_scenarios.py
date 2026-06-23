"""Pure-unit tests for scripts/_macro_scenarios.py (Macro Ch.6 probe scenarios).

Pins the authored teaching transcripts to the structure the probe + design
require: 5 macro problems x 3 variations (strong/partial/weak), each non-empty;
a per-problem Hoot intro transcript that steers infer_concept_id; and the
case-3 invariant — Q4 strong states the base deflator relation, asserts the
price index IS the deflator, rearranges to realGDP = nomGDP/(PI/100), and
computes. No DB, no network.
"""

from __future__ import annotations

import pytest

import scripts._macro_scenarios as ms


def test_five_macro_problems_in_order():
    assert ms.MACRO_PROBLEM_IDS == (
        "gdp_identity",
        "net_exports_sign",
        "nnp_chain",
        "real_gdp_from_deflator",
        "real_gdp_growth",
    )


def test_variation_order():
    assert ms.MACRO_VARIATIONS == ("strong", "partial", "weak")


@pytest.mark.parametrize("problem_id", ms.MACRO_PROBLEM_IDS)
@pytest.mark.parametrize("variation", ms.MACRO_VARIATIONS)
def test_every_cell_has_nonempty_messages(problem_id, variation):
    msgs = ms.macro_variation_messages(problem_id, variation)
    assert isinstance(msgs, list)
    assert msgs, f"{problem_id}/{variation} is empty"
    assert all(isinstance(m, str) and m.strip() for m in msgs)


@pytest.mark.parametrize("problem_id", ms.MACRO_PROBLEM_IDS)
def test_every_problem_has_intro_transcript(problem_id):
    transcript = ms.macro_transcript(problem_id)
    assert isinstance(transcript, str) and transcript.strip()


def test_macro_variation_messages_unknown_problem_returns_none():
    assert ms.macro_variation_messages("not_a_macro_problem", "strong") is None


def test_macro_variation_messages_unknown_variation_raises():
    with pytest.raises(KeyError):
        ms.macro_variation_messages("gdp_identity", "brilliant")


def test_macro_transcript_unknown_problem_returns_none():
    assert ms.macro_transcript("not_a_macro_problem") is None


# --- the case-3 invariant on Q4 strong --------------------------------------


def test_q4_strong_states_base_deflator_relation():
    msgs = " ".join(ms.macro_variation_messages("real_gdp_from_deflator", "strong"))
    # base deflator relation deflator = (nomGDP/realGDP)*100
    assert "(nomGDP/realGDP)*100" in msgs


def test_q4_strong_asserts_price_index_is_deflator():
    msgs = " ".join(ms.macro_variation_messages("real_gdp_from_deflator", "strong")).lower()
    assert "price index" in msgs and "deflator" in msgs


def test_q4_strong_rearranges_to_real_gdp_form():
    msgs = " ".join(ms.macro_variation_messages("real_gdp_from_deflator", "strong"))
    # the rearranged/solved form whose USES-edge attachment is the observable
    assert "nomGDP/(PI/100)" in msgs


def test_q4_strong_computes_the_answer():
    msgs = " ".join(ms.macro_variation_messages("real_gdp_from_deflator", "strong"))
    assert "2859.5" in msgs


def test_q4_weak_deflates_wrong_direction():
    # the misconception: multiply by the price index instead of dividing
    msgs = " ".join(ms.macro_variation_messages("real_gdp_from_deflator", "weak")).lower()
    assert "multiply" in msgs


# --- per-concept misconception is voiced in each weak variation -------------


def test_weak_variations_voice_their_misconception():
    checks = {
        "gdp_identity": "transfer payments",   # misc.includes_transfers
        "net_exports_sign": "transfer",        # misc.includes_transfers
        "nnp_chain": "depreciation",           # misc.gross_for_net (forgets depreciation)
        "real_gdp_from_deflator": "multiply",  # misc.deflate_wrong_direction
        "real_gdp_growth": "nominal",          # misc.nominal_for_real
    }
    for problem_id, needle in checks.items():
        weak = " ".join(ms.macro_variation_messages(problem_id, "weak")).lower()
        assert needle in weak, f"{problem_id} weak does not voice its misconception ({needle!r})"


def test_partial_is_shorter_than_strong():
    # partial omits one reference node/edge, so it states fewer turns than strong
    for problem_id in ms.MACRO_PROBLEM_IDS:
        strong = ms.macro_variation_messages(problem_id, "strong")
        partial = ms.macro_variation_messages(problem_id, "partial")
        assert len(partial) <= len(strong)
