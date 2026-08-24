"""Level 3 fills the containers the codebase already carries — and only those.

§2.5's table: `topics[].misconceptions`, the artifact's `misconceptions` array,
the scorecard's *watch out* list and the provenance `docks[]` are all built and
empty today. Level 3 populates them with `dock_points: 0.0` — "we saw this, it
cost nothing" — and moves no number. Level 2 populates none of them.
"""

from __future__ import annotations

import pytest

from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.overseer.rubric import score_to_band

pytestmark = pytest.mark.unit


async def test_level_3_populates_topics_misconceptions(monkeypatch):
    out, _ = await wf.run_done(monkeypatch, level=3)

    by_key = {t["canonical_key"]: t for t in out["topics"]}
    assert by_key["eq1"]["misconceptions"] == [
        {
            "canonical_key": "eq1",
            "resolved": False,
            "dock_points": 0.0,
            "evidence_span": wf.MATERIAL_QUOTE,
        }
    ]
    # Only the corroborated node; the clean node stays empty.
    assert by_key["c1"]["misconceptions"] == []


async def test_level_3_moves_no_number(monkeypatch):
    """Level 3's ship condition: containers fill, the grade does not move.
    `misconception_dock` stays 0.0 because the ceiling is level 4 only."""
    level2, _ = await wf.run_done(monkeypatch, level=2)
    level3, started = await wf.run_done(monkeypatch, level=3)

    assert level3["rubric"]["overall"] == level2["rubric"]["overall"]
    assert level3["xp_earned"] == level2["xp_earned"]
    topic_score = started["write_artifacts"].await_args.kwargs["topic_score"]
    assert topic_score.misconception_dock == 0.0
    assert topic_score.score == 100
    assert [(t.canonical_key, t.credit, t.weight, t.status) for t in topic_score.topics] == [
        ("eq1", 1.0, 0.5, "covered"),
        ("c1", 1.0, 0.5, "covered"),
    ]


async def test_level_3_serves_exactly_one_topic_score_result(monkeypatch):
    """The level-3 rescore SUPERSEDES the raw result; the served payload, the
    provenance and the artifact must never carry two divergent scorings."""
    out, started = await wf.run_done(monkeypatch, level=3)

    artifact_topics = started["write_artifacts"].await_args.kwargs["topic_score"]
    from apollo.overseer.topic_score_serialize import serialize_topics

    assert out["topics"] == serialize_topics(artifact_topics)
    assert out["grading_provenance"]["topics"] == out["topics"]


async def test_level_3_populates_artifact_misconceptions(monkeypatch):
    out, started = await wf.run_done(monkeypatch, level=3, real_artifact=True)

    from apollo.grading.artifact_build import build_llm_artifact

    payload = build_llm_artifact(
        coverage=started["write_artifacts"].await_args.kwargs["coverage"],
        rubric=started["write_artifacts"].await_args.kwargs["rubric"],
        latency_ms=0,
        clarification_trace=[],
        topic_score=started["write_artifacts"].await_args.kwargs["topic_score"],
    )
    assert payload["misconceptions"] == [
        {"canonical_key": "eq1", "resolved": False, "evidence_span": wf.MATERIAL_QUOTE}
    ]
    assert out["scorecard"] is not None


async def test_level_3_scorecard_watch_out_is_served(monkeypatch):
    """`scorecard._watch_out` needed no change (§2.5) — it already reads
    `{canonical_key, evidence_span}` off the artifact array. Proved by running
    the REAL builder + the REAL renderer, not by re-stating the keys."""
    out, _ = await wf.run_done(monkeypatch, level=3, real_artifact=True)

    assert out["scorecard"]["watch_out"] == [{"key": "eq1", "quote": wf.MATERIAL_QUOTE}]
    assert out["scorecard"]["watch_out_status"] == "checked"


async def test_level_3_docks_carry_zero_points(monkeypatch):
    """`grading_provenance["docks"]` becomes non-empty at `points: 0.0`. That is
    intended provenance, not a bug to special-case away: the record says we saw
    the contradiction and charged nothing for it."""
    out, _ = await wf.run_done(monkeypatch, level=3)

    assert out["grading_provenance"]["docks"] == [
        {
            "key": "eq1",
            "points": 0.0,
            "evidence_span": wf.MATERIAL_QUOTE,
            "resolved": False,
        }
    ]
    assert out["grading_provenance"]["score_before_dock"] == 1.0


@pytest.mark.parametrize("level", [0, 1, 2])
async def test_level_2_populates_nothing(monkeypatch, level):
    out, _ = await wf.run_done(monkeypatch, level=level, real_artifact=True)

    assert all(topic["misconceptions"] == [] for topic in out["topics"])
    assert out["grading_provenance"]["docks"] == []
    assert out["scorecard"]["watch_out"] == []


async def test_uncorroborated_finding_never_reaches_a_container(monkeypatch):
    """Only the corroborated rung is nameable. A finding the second reader
    denied is logged and dropped, never surfaced to the student."""
    out, _ = await wf.run_done(
        monkeypatch,
        level=3,
        real_artifact=True,
        wrongness_map=wf.second_reader(contradicted=False),
    )

    assert all(topic["misconceptions"] == [] for topic in out["topics"])
    assert out["scorecard"]["watch_out"] == []


async def test_a_resolved_finding_is_never_surfaced_as_a_misconception(monkeypatch):
    """S2′ requires NOT corrected_later, so a student who fixed their claim is
    never shown a misconception line for it — they get the XP bonus instead."""
    out, _ = await wf.run_done(
        monkeypatch,
        level=3,
        real_artifact=True,
        wrongness_map=wf.second_reader(corrected_later=True),
    )

    assert all(topic["misconceptions"] == [] for topic in out["topics"])
    assert out["scorecard"]["watch_out"] == []


async def test_level_4_is_the_only_rung_that_moves_the_score(monkeypatch):
    """The DARK ceiling, wired but unreachable: nothing sets level 4. Proves
    `ceiling_active=(level >= 4)` really reaches the scorer, and P-1 — the
    `min()` lands on a B+, never a D or an F."""
    level3, _ = await wf.run_done(monkeypatch, level=3)
    level4, started = await wf.run_done(monkeypatch, level=4)

    # Whole-dict: score + letter + the additive study-prep band, nothing else.
    assert level3["rubric"]["overall"] == {
        "score": 100,
        "letter": "A+",
        "band": score_to_band(100),
    }
    assert level4["rubric"]["overall"] == {
        "score": 84,
        "letter": "B+",
        "band": score_to_band(84),
    }
    topic_score = started["write_artifacts"].await_args.kwargs["topic_score"]
    assert topic_score.misconception_dock == 16.0
    assert level4["grading_provenance"]["docks"][0]["points"] == 16.0


async def test_a_soft_failed_container_pass_keeps_the_raw_grade(monkeypatch):
    """The container pass is additive; if it raises, the student keeps the score
    the raw pass produced rather than losing a grade to a display feature."""
    from unittest.mock import patch

    from apollo.overseer.topic_score import compute_topic_score as real_compute

    calls = {"n": 0}

    def _second_call_explodes(**kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("container pass blew up")
        return real_compute(**kwargs)

    out, _ = await wf.run_done(
        monkeypatch,
        level=3,
        extra_patches=[
            patch("apollo.handlers.done.compute_topic_score", side_effect=_second_call_explodes)
        ],
    )

    assert calls["n"] == 2
    assert out["rubric"]["overall"]["score"] == 100
    assert all(topic["misconceptions"] == [] for topic in out["topics"])
