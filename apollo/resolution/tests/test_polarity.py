# apollo/resolution/tests/test_polarity.py
from apollo.resolution.polarity import polarity_allows_match


def test_negation_mismatch_rejected() -> None:
    d = polarity_allows_match("pressure does not increase", "pressure increases")
    assert d.allowed is False and d.reason == "negation_mismatch"


def test_direction_mismatch_rejected() -> None:
    d = polarity_allows_match("velocity decreases downstream", "velocity increases downstream")
    assert d.allowed is False and d.reason == "direction_mismatch"


def test_inverse_proportional_rejected() -> None:
    d = polarity_allows_match(
        "pressure is proportional to volume",
        "pressure is inversely proportional to volume",
    )
    assert d.allowed is False and d.reason == "direction_mismatch"


def test_litotes_allowed_unknown_polarity() -> None:
    d = polarity_allows_match("there is no change in density", "density is constant")
    assert d.allowed is True and d.reason == "same_or_unknown"


def test_neutral_text_allowed() -> None:
    assert (
        polarity_allows_match("the fluid is incompressible", "incompressible flow").allowed is True
    )


def test_litotes_intervening_noun_allowed() -> None:
    # "no elevation change" is litotes ("elevation is constant") — the null-change
    # word ("change") follows the negation with an intervening noun. The guard
    # must look past the intervening noun and PASS to NLI (not fire a mismatch).
    d = polarity_allows_match(
        "the conduit has no elevation change so height stays the same",
        "both sections are at the same height",
    )
    assert d.allowed is True and d.reason == "same_or_unknown"


def test_litotes_adjective_before_null_change_allowed() -> None:
    # "no significant difference" — adjective between negation and null-change word.
    d = polarity_allows_match(
        "there is no significant difference in density",
        "density is uniform",
    )
    assert d.allowed is True and d.reason == "same_or_unknown"


def test_negation_with_distant_null_change_word_still_blocks() -> None:
    # Precision guard: the litotes window is BOUNDED. A genuine negation
    # ("does not increase") is NOT excused merely because a null-change word
    # appears later in the sentence, outside the window.
    d = polarity_allows_match(
        "the density does not increase despite the later pressure change",
        "the density increases",
    )
    assert d.allowed is False and d.reason == "negation_mismatch"


def test_non_no_not_negation_still_counts() -> None:
    # A negation that is NOT "no"/"not" (e.g. "never") skips the litotes window
    # entirely and always counts — the polarity XOR must still fire.
    d = polarity_allows_match("the pressure never increases", "the pressure increases")
    assert d.allowed is False and d.reason == "negation_mismatch"


def test_no_effect_vs_effect_rejected() -> None:
    # Regression: "no effect" is absence-vs-presence, not a zero-magnitude litotes.
    # "effect"/"effects" must NOT be in _NULL_CHANGE, so "no effect" fires as
    # a genuine negation mismatch rather than passing to NLI.
    d = polarity_allows_match(
        "the intervention has no effect on pressure",
        "the intervention has an effect on pressure",
    )
    assert d.allowed is False and d.reason == "negation_mismatch"
