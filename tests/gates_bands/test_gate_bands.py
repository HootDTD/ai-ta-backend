"""§A.5 inertness gate for the proficiency-band wire field.

The band is ADDITIVE and unconditional: no flag selects it, so "inert" cannot be
asserted the way P3.2's G-L1 asserts it (two runs of one build, the ladder rung
as the only free variable). It is asserted against BYTES RECORDED ON THE BASE
COMMIT instead — ``base_blobs.json``, captured at ``origin/staging@1f1eafc7``
before ``score_to_band`` existed, over identical seeded state for every entry in
``_baseline.CASES``.

The assertion is a WHOLE-blob path diff, never a projection. §A.5 forbids partial
comparisons for a concrete reason: §2.5's absent-axis hazard moves
``llm_rubric.overall`` one rung BELOW the field a narrow "did `served_overall`
change?" assertion would look at, and it would sail straight past it. So every
differing path in all four blobs is enumerated and the SET is compared against
:data:`EXPECTED_BAND_PATHS` — a diff this change did not intend cannot be
absorbed, in either direction.

**Both serving branches are covered, and that is deliberate.** The P3.2 harness
leaves the real ``compute_topic_score`` on the path (its patch is in the drop
set), and it always computes on this fixture, so every gate built on that harness
lands on ``serve_topic_score = True`` — i.e. on ``_served_overall_block`` alone.
The soft-fail branch (``_with_band`` over the axis blend) is the band change's
scope extension and had no whole-blob coverage at all until the ``L*-softfail``
cases below, which drive it through ``run_gate_done(topic_score_side_effect=...)``.
Their sharpest claim is a NEGATIVE one: on that branch
``diagnostic_report.rubric`` — the RAW rubric, persisted as the grade of record
and read by every teacher projection — must show ZERO diff, which is what proves
``_with_band`` decorates a copy instead of mutating in place.

The ladder rungs being part of the fixture set buys one more property: the band
must be inert with respect to the rung too.
"""

from __future__ import annotations

import pytest

from apollo.overseer.rubric import BAND_TOKENS, score_to_band
from tests.gates_bands import _baseline
from tests.gates_p32._harness import blob

pytestmark = pytest.mark.unit

#: Every path the band change is permitted to add, per blob. Identical on BOTH
#: serving branches — that sameness is itself part of the contract. Anything else
#: in a diff (added, removed, or changed) fails the gate.
EXPECTED_BAND_PATHS: dict[str, set[tuple[str, str]]] = {
    "student_response": {("rubric.overall.band", "added")},
    "diagnostic_report": {("served_overall.band", "added")},
    "score_details": set(),
    "artifact": set(),
}

#: Parametrization id -> case, so a failure names the branch it happened on.
_CASE_IDS = [case.case_id for case in _baseline.CASES]


async def _live_blobs(monkeypatch, case: _baseline.Case) -> dict[str, object]:
    return _baseline.blobs_of(await _baseline.run_case(monkeypatch, case))


# --------------------------------------------------------------------------- #
# Methodology controls — a gate that cannot fail asserts nothing               #
# --------------------------------------------------------------------------- #


def test_the_baseline_covers_every_case_and_every_blob():
    """A truncated baseline would make the gate below silently vacuous — and in
    particular, a baseline missing the soft-fail cases would leave `_with_band`
    uncovered exactly the way it was before this gate grew them."""
    baseline = _baseline.load_baseline()

    assert set(baseline) == set(_CASE_IDS)
    assert [case.case_id for case in _baseline.CASES if case.soft_fail] == [
        "L0-softfail",
        "L3-softfail",
    ]
    for case_id, blobs in baseline.items():
        assert set(blobs) == set(_baseline.BLOB_NAMES), case_id


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

    for case_id, blobs in baseline.items():
        assert "band" not in blobs["student_response"]["rubric"]["overall"], case_id
        assert "band" not in blobs["diagnostic_report"]["served_overall"], case_id
        assert "band" not in blobs["diagnostic_report"]["rubric"]["overall"], case_id
        assert blobs["student_response"]["scorecard"]["band"] == "Strong", case_id
        for token in BAND_TOKENS:
            assert f'"{token}"' not in blob(blobs), (case_id, token)


@pytest.mark.parametrize(("served_id", "soft_id"), [("L0", "L0-softfail"), ("L3", "L3-softfail")])
def test_the_baseline_really_recorded_two_different_serving_branches(served_id, soft_id):
    """Otherwise the soft-fail cases would be duplicates of the topic-score runs
    and would prove nothing about `_with_band`.

    The branch fingerprint is STRUCTURAL, not numeric: a soft-failed topic score
    means `topics` is absent from the student payload (absent, not null) and
    `score_details.topic_score` was never written. It is deliberately NOT a
    comparison of the overall's numbers — on this fixture the topic score and
    the axis blend agree at 100/A+, so a value comparison would report the two
    branches as identical and this control would be worthless.
    """
    baseline = _baseline.load_baseline()
    served = baseline[served_id]
    soft = baseline[soft_id]

    assert "topics" in served["student_response"]
    assert "topics" not in soft["student_response"]
    assert isinstance(served["score_details"].get("topic_score"), dict)
    assert "topic_score" not in soft["score_details"]
    # ...and on the soft-fail branch the served overall IS the raw rubric's
    # overall, which is exactly why `_with_band` has to copy before decorating.
    assert (
        soft["student_response"]["rubric"]["overall"]
        == soft["diagnostic_report"]["rubric"]["overall"]
    )


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


@pytest.mark.parametrize("case", _baseline.CASES, ids=_CASE_IDS)
async def test_gate_bands_the_only_diff_vs_base_is_the_added_band(monkeypatch, case):
    """Whole-blob, all four blobs, at every rung and on BOTH serving branches:
    the diff vs the pre-band recording is EXACTLY the added `band` key.

    On the soft-fail cases this subsumes the raw-rubric claim — `score_details.
    llm_rubric` and `diagnostic_report.rubric` are inside the compared blobs, so
    a band leaking into the persisted grade of record shows up here as an
    unexpected `added` path.
    """
    base = _baseline.load_baseline()[case.case_id]
    live = await _live_blobs(monkeypatch, case)

    for name in _baseline.BLOB_NAMES:
        assert set(_baseline.diff_paths(base[name], live[name])) == EXPECTED_BAND_PATHS[name], name


@pytest.mark.parametrize("case", _baseline.CASES, ids=_CASE_IDS)
async def test_gate_bands_stripped_blobs_are_byte_identical_to_base(monkeypatch, case):
    """The same claim as canonical-JSON equality rather than a path set, so a
    defect in `diff_paths` itself cannot hide a difference: pop the added key
    back off and the bytes must be the base's bytes."""
    base = _baseline.load_baseline()[case.case_id]
    live = await _live_blobs(monkeypatch, case)

    assert live["student_response"]["rubric"]["overall"].pop("band")
    assert live["diagnostic_report"]["served_overall"].pop("band")
    for name in _baseline.BLOB_NAMES:
        assert blob(live[name]) == blob(base[name]), name


@pytest.mark.parametrize("case", _baseline.CASES, ids=_CASE_IDS)
async def test_gate_bands_the_served_band_matches_the_served_score(monkeypatch, case):
    """Inertness alone would be satisfied by a band that is always wrong. The
    served band must be `score_to_band` of the score on the SAME payload, and
    the persisted snapshot must record exactly what was served."""
    live = await _live_blobs(monkeypatch, case)
    overall = live["student_response"]["rubric"]["overall"]

    assert overall["band"] == score_to_band(overall["score"])
    assert live["diagnostic_report"]["served_overall"] == overall


@pytest.mark.parametrize(
    "case", [c for c in _baseline.CASES if c.soft_fail], ids=["L0-softfail", "L3-softfail"]
)
async def test_gate_bands_the_soft_fail_branch_leaves_the_raw_rubric_band_free(monkeypatch, case):
    """Stated positively as well as by diff, because it is the single claim the
    scope extension rests on: `_with_band` decorates a COPY. The served overall
    gains the band; the RAW rubric persisted in `diagnostic_report["rubric"]`
    (and mirrored into the artifact's `llm_rubric`) keeps the exact
    `compute_rubric` shape, band-free, on every axis block too."""
    live = await _live_blobs(monkeypatch, case)

    served = live["student_response"]["rubric"]
    raw = live["diagnostic_report"]["rubric"]

    assert "band" in served["overall"]
    assert "band" not in raw["overall"]
    assert served["overall"] == {**raw["overall"], "band": score_to_band(raw["overall"]["score"])}
    # ...and nothing anywhere else in the persisted rubric or its artifact copy.
    assert '"band"' not in blob(raw)
    assert '"band"' not in blob(live["score_details"]["llm_rubric"])
    # The axis blocks are carried over untouched, key for key.
    assert {k: v for k, v in served.items() if k != "overall"} == {
        k: v for k, v in raw.items() if k != "overall"
    }


async def test_gate_bands_the_ladder_still_moves_what_it_used_to_move(monkeypatch):
    """The counter-proof that the baseline is a live signal: level 4's ceiling
    lowers the served score (100 -> 84) and, with it, the band. If the rungs had
    collapsed into each other, every assertion above would be comparing the same
    payload four times."""
    level3 = await _live_blobs(monkeypatch, _baseline.Case("L3", 3))
    level4 = await _live_blobs(monkeypatch, _baseline.Case("L4", 4))

    assert level3["student_response"]["rubric"]["overall"]["score"] == 100
    assert level4["student_response"]["rubric"]["overall"]["score"] == 84
    assert level3["student_response"]["rubric"]["overall"]["band"] == "advanced"
    assert level4["student_response"]["rubric"]["overall"]["band"] == "intermediate"


async def test_gate_bands_letter_survives_everywhere_the_band_appears(monkeypatch):
    """The standing constraint: `band` is added, `letter` is never removed."""
    live = await _live_blobs(monkeypatch, _baseline.Case("L0", 0))

    assert set(live["student_response"]["rubric"]["overall"]) == {"score", "letter", "band"}
    assert set(live["diagnostic_report"]["served_overall"]) == {"score", "letter", "band"}
    assert live["score_details"]["topic_score"]["letter"]
    assert live["score_details"]["llm_rubric"]["overall"]["letter"]
