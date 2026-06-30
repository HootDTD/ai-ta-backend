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


def test_no_effect_vs_effect_rejected() -> None:
    # Regression: "no effect" is absence-vs-presence, not a zero-magnitude litotes.
    # "effect"/"effects" must NOT be in _NULL_CHANGE, so "no effect" fires as
    # a genuine negation mismatch rather than passing to NLI.
    d = polarity_allows_match(
        "the intervention has no effect on pressure",
        "the intervention has an effect on pressure",
    )
    assert d.allowed is False and d.reason == "negation_mismatch"
