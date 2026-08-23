"""§A.5 inertness gate for the proficiency-band wire field.

The band is ADDITIVE and unconditional: no flag selects it, so "inert" cannot be
asserted the way P3.2's G-L1 asserts it (two runs of one build, the ladder rung
as the only free variable). It is asserted against BYTES RECORDED ON THE BASE
COMMIT instead — ``base_blobs.json``, captured at ``origin/staging@1f1eafc7``
before ``score_to_band`` existed, over the identical seeded state at every rung
of the wrongness ladder.

The assertion is a WHOLE-blob path diff, never a projection. §A.5 forbids partial
comparisons for a concrete reason: §2.5's absent-axis hazard moves
``llm_rubric.overall`` one rung BELOW the field a narrow "did `served_overall`
change?" assertion would look at, and it would sail straight past it. So every
differing path in all four blobs is enumerated and the SET is compared against
:data:`EXPECTED_BAND_PATHS` — a diff this change did not intend cannot be
absorbed, in either direction.

The ladder rungs are the fixture set, which buys a second property for free: the
band must be inert with respect to the rung too (levels 0-3 differ from each
other in the baseline exactly as they did before, and the band adds the same two
paths at every one of them).
"""

from __future__ import annotations

import os

import pytest

from apollo.overseer.rubric import BAND_TOKENS, score_to_band
from tests.gates_bands import _baseline
from tests.gates_p32._harness import blob, run_gate_done

pytestmark = pytest.mark.unit

#: Every path the band change is permitted to add, per blob. Anything else in a
#: diff — added, removed, or changed — fails the gate.
EXPECTED_BAND_PATHS: dict[str, set[tuple[str, str]]] = {
    "student_response": {("rubric.overall.band", "added")},
    "diagnostic_report": {("served_overall.band", "added")},
    "score_details": set(),
    "artifact": set(),
}


async def _live_blobs(monkeypatch, level: int) -> dict[str, object]:
    return _baseline.blobs_of(await run_gate_done(monkeypatch, level=level))


# --------------------------------------------------------------------------- #
# Methodology controls — a gate that cannot fail asserts nothing               #
# --------------------------------------------------------------------------- #


def test_the_baseline_covers_every_rung_and_every_blob():
    """A truncated baseline would make the gate below silently vacuous."""
    baseline = _baseline.load_baseline()

    assert set(baseline) == {str(level) for level in _baseline.LEVELS}
    for level, blobs in baseline.items():
        assert set(blobs) == set(_baseline.BLOB_NAMES), level


def test_the_baseline_is_a_PRE_band_recording():
    """The one property that makes it a baseline at all: it carries no
    proficiency band. Recapturing on a branch that already has the change would
    record the change as its own baseline and every assertion here would pass
    for the wrong reason.

    NOTE the pre-existing name collision this pins: ``scorecard.band`` already
    existed and is a DIFFERENT vocabulary — the campaign scorecard's capitalized
    Strong/Proficient/Developing/Beginning table (``projections/scorecard.py``,
    env-tunable, computed off ``scores.composite``), untouched by this change.
    Two keys named ``band`` now ride the same student payload; the assertion
    below is deliberately written so the collision is visible rather than
    swallowed by a blanket string search.
    """
    baseline = _baseline.load_baseline()

    for level, blobs in baseline.items():
        assert "band" not in blobs["student_response"]["rubric"]["overall"], level
        assert "band" not in blobs["diagnostic_report"]["served_overall"], level
        assert blobs["student_response"]["scorecard"]["band"] == "Strong", level
        for token in BAND_TOKENS:
            assert f'"{token}"' not in blob(blobs), (level, token)


def test_the_diff_primitive_reports_every_kind_of_change():
    """`diff_paths` is the whole gate. Exercised directly so a failure to detect
    is a failure HERE rather than a false green below."""
    base = {"keep": 1, "gone": 2, "deep": {"n": [1, 2]}, "list": [1]}
    live = {"keep": 1, "new": 3, "deep": {"n": [1, 9]}, "list": [1, 2]}

    assert set(_baseline.diff_paths(base, live)) == {
        ("deep.n[1]", "changed"),
        ("gone", "removed"),
        ("list", "changed"),
        ("new", "added"),
    }
    assert _baseline.diff_paths(base, base) == []


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", _baseline.LEVELS)
async def test_gate_bands_the_only_diff_vs_base_is_the_added_band(monkeypatch, level):
    """Whole-blob, all four blobs, at every rung: the diff vs the pre-band
    recording is EXACTLY the added `band` key and nothing else."""
    base = _baseline.load_baseline()[str(level)]
    live = await _live_blobs(monkeypatch, level)

    for name in _baseline.BLOB_NAMES:
        assert set(_baseline.diff_paths(base[name], live[name])) == EXPECTED_BAND_PATHS[name], name


@pytest.mark.parametrize("level", _baseline.LEVELS)
async def test_gate_bands_stripped_blobs_are_byte_identical_to_base(monkeypatch, level):
    """The same claim as canonical-JSON equality rather than a path set, so a
    defect in `diff_paths` itself cannot hide a difference: pop the added key
    back off and the bytes must be the base's bytes."""
    base = _baseline.load_baseline()[str(level)]
    live = await _live_blobs(monkeypatch, level)

    assert live["student_response"]["rubric"]["overall"].pop("band")
    assert live["diagnostic_report"]["served_overall"].pop("band")
    for name in _baseline.BLOB_NAMES:
        assert blob(live[name]) == blob(base[name]), name


@pytest.mark.parametrize("level", _baseline.LEVELS)
async def test_gate_bands_the_served_band_matches_the_served_score(monkeypatch, level):
    """Inertness alone would be satisfied by a band that is always wrong. The
    served band must be `score_to_band` of the score on the SAME payload, and
    the persisted snapshot must record exactly what was served."""
    live = await _live_blobs(monkeypatch, level)
    overall = live["student_response"]["rubric"]["overall"]

    assert overall["band"] == score_to_band(overall["score"])
    assert live["diagnostic_report"]["served_overall"] == overall


async def test_gate_bands_the_ladder_still_moves_what_it_used_to_move(monkeypatch):
    """The counter-proof that the baseline is a live signal: level 4's ceiling
    lowers the served score (100 -> 84) and, with it, the band. If the rungs had
    collapsed into each other, every assertion above would be comparing the same
    payload four times."""
    level3 = await _live_blobs(monkeypatch, level=3)
    level4 = await _live_blobs(monkeypatch, level=4)

    assert level3["student_response"]["rubric"]["overall"]["score"] == 100
    assert level4["student_response"]["rubric"]["overall"]["score"] == 84
    assert level3["student_response"]["rubric"]["overall"]["band"] == "advanced"
    assert level4["student_response"]["rubric"]["overall"]["band"] == "intermediate"


async def test_gate_bands_letter_survives_everywhere_the_band_appears(monkeypatch):
    """The standing constraint: `band` is added, `letter` is never removed."""
    live = await _live_blobs(monkeypatch, level=0)

    assert set(live["student_response"]["rubric"]["overall"]) == {"score", "letter", "band"}
    assert set(live["diagnostic_report"]["served_overall"]) == {"score", "letter", "band"}
    assert live["score_details"]["topic_score"]["letter"]
    assert live["score_details"]["llm_rubric"]["overall"]["letter"]


# --------------------------------------------------------------------------- #
# Baseline regeneration — a deliberate act on the BASE commit, never in CI     #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not os.environ.get("APOLLO_BANDS_CAPTURE_BASELINE"),
    reason="baseline capture belongs on the BASE commit; see tests/gates_bands/_baseline.py",
)
async def test_capture_baseline(monkeypatch):
    _baseline.write_baseline(await _baseline.capture(monkeypatch))
