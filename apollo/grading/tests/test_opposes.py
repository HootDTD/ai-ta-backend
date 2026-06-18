"""WU-4B2 — the opposes-map builder tests.

Pure — no DB/LLM/network. ``build_opposes_map`` turns the closed candidate set
into the ``{misconception_key: opposed_entity_key}`` lookup the §6.5 conflict
rows key on (a CONTRADICTION on a misconception entity vs a COVERED on the
entity it opposes is an opposed pair)."""

from __future__ import annotations

from apollo.grading.opposes import build_opposes_map

from ._builders import candidate, misc_candidate


def test_build_opposes_map_maps_misconception_to_opposed():
    """A misconception candidate's canonical_key -> its opposes_key."""
    candidates = (misc_candidate("misc.density_ignored", "cond.incompressibility"),)

    assert build_opposes_map(candidates) == {
        "misc.density_ignored": "cond.incompressibility"
    }


def test_build_opposes_map_skips_non_misconception_and_none_opposes():
    """A non-misconception candidate and a misconception with opposes_key=None
    BOTH contribute nothing (only opposing misconceptions are detectable pairs)."""
    candidates = (
        candidate("eq.bernoulli"),                       # not a misconception
        misc_candidate("misc.no_opposes", None),         # misconception, no opposes
        misc_candidate("misc.density_ignored", "cond.incompressibility"),
    )

    assert build_opposes_map(candidates) == {
        "misc.density_ignored": "cond.incompressibility"
    }


def test_build_opposes_map_is_immutable_mapping():
    """The returned mapping equals the expected dict and the input tuple is not
    mutated (immutability rule)."""
    candidates = (
        misc_candidate("misc.a", "eq.a"),
        misc_candidate("misc.b", "eq.b"),
    )

    result = build_opposes_map(candidates)

    assert result == {"misc.a": "eq.a", "misc.b": "eq.b"}
    # inputs untouched
    assert candidates == (
        misc_candidate("misc.a", "eq.a"),
        misc_candidate("misc.b", "eq.b"),
    )
