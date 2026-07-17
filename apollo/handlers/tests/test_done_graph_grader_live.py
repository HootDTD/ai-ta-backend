"""Campaign-plan Task A4 — the ``APOLLO_GRAPH_GRADER_LIVE`` promotion flag +
any-exception -> LLM-fallback hardening.

Pure unit tests: every OLD-path collaborator is mocked deterministically
(``_old_path_patches``), Neo4j is a MagicMock, and both ``run_graph_simulation``
and ``write_artifacts`` are patched on the ``done`` module so no real chain /
DB write / live LLM runs.

Byte-identity guard: flag OFF must reproduce the exact OLD-path golden
regardless of what the (never-called-for-promotion) shadow mock would have
returned.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.grading.artifact_build import GRADER_USED_GRAPH, GRADER_USED_LLM_FALLBACK
from apollo.handlers import done as done_mod
from apollo.handlers.done import _graph_grader_live_enabled, _project_mastery, handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches, _rerun_inputs

pytestmark = pytest.mark.unit


def _shadow_result(*, abstained: bool = False) -> MagicMock:
    """A fabricated ShadowGradeResult sentinel carrying a DISTINCT graph_sim
    rubric + constrained-diagnostic narrative (so promotion is observable) and
    an ``audited.abstained`` flag driving the LIVE promotion gate."""
    result = MagicMock(name="ShadowGradeResult")
    result.graph_sim_rubric = {"overall": {"score": 88, "letter": "B+"}}
    result.diagnostic = MagicMock(narrative="graph-sim narrative")
    result.audited = MagicMock(
        abstained=abstained, abstention_reasons=("normalization_confidence",)
    )
    return result


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_GRADER_LIVE", raising=False)
    monkeypatch.delenv("APOLLO_GRADING_ARTIFACT_ENABLED", raising=False)
    yield


_FAKE_CANONICAL_PAYLOAD = {
    "grader_used": GRADER_USED_GRAPH,
    "scores": {"composite": 0.9},
    "node_ledger": [],
    "misconceptions": [],
    "clarification_trace": [],
}


async def _run_with_flags(
    monkeypatch,
    *,
    shadow: bool,
    live,
    artifact: bool = False,
    shadow_return=None,
    shadow_side_effect=None,
    write_artifacts_return=None,
):
    """Run handle_done with the shadow + A4-live (+ optional artifact) flags
    set, ``run_graph_simulation``/``build_rerun_inputs``/``write_artifacts``
    patched on the ``done`` module."""
    if shadow:
        monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    if live is not None:
        monkeypatch.setenv("APOLLO_GRAPH_GRADER_LIVE", live)
    if artifact:
        monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, _attempt, patches = _old_path_patches()
    payload = {"declared_paths": [["a"]], "symbolic_mappings": {"d": "2*r"}}
    if shadow_side_effect is not None:
        shadow_mock = AsyncMock(side_effect=shadow_side_effect)
    else:
        shadow_mock = AsyncMock(return_value=shadow_return)
    write_artifacts_mock = AsyncMock(return_value=write_artifacts_return)

    rerun = _rerun_inputs(problem_payload=payload)

    with (
        patch("apollo.handlers.done.run_graph_simulation", new=shadow_mock),
        patch("apollo.handlers.done.build_rerun_inputs", new=AsyncMock(return_value=rerun)),
        patch("apollo.handlers.done.write_artifacts", new=write_artifacts_mock),
    ):
        for p in patches:
            p.start()
        try:
            out = await handle_done(db=db, neo=MagicMock(), session_id=11)
        finally:
            for p in reversed(patches):
                p.stop()
    return out, shadow_mock, write_artifacts_mock


def test_live_flag_parsing(monkeypatch):
    for truthy in ("1", "true", "TRUE", "Yes", "yes"):
        monkeypatch.setenv("APOLLO_GRAPH_GRADER_LIVE", truthy)
        assert _graph_grader_live_enabled() is True
    for falsy in ("0", "false", "no", "", "off", "maybe"):
        monkeypatch.setenv("APOLLO_GRAPH_GRADER_LIVE", falsy)
        assert _graph_grader_live_enabled() is False
    monkeypatch.delenv("APOLLO_GRAPH_GRADER_LIVE", raising=False)
    assert _graph_grader_live_enabled() is False


def test_live_flag_constant_name():
    assert done_mod._GRAPH_GRADER_LIVE_FLAG == "APOLLO_GRAPH_GRADER_LIVE"


async def test_flag_off_byte_identical_to_shadow_golden(monkeypatch):
    """(a) LIVE off ⇒ response is the exact OLD-path golden, regardless of
    what a healthy shadow would have promoted."""
    out, shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="false",
        shadow_return=_shadow_result(),
    )
    shadow_mock.assert_awaited_once()
    assert out == {
        "rubric": {"overall": {"score": 0.5}},
        "diagnostic_narrative": "narrative",
        "coverage": {},
        "progress": {
            "xp_earned": 10,
            "xp_before": 0,
            "xp_after": 10,
            "level_before": 1,
            "level_after": 1,
            "level_up": False,
            "title_after": "Novice",
            "level_progress_pct": 0.1,
            "xp_to_next_level": 90,
        },
        "xp_earned": 10,
        "xp_before": 0,
        "xp_after": 10,
        "level_before": 1,
        "level_after": 1,
        "level_up": False,
        "transcript": [],
        "grading_provenance": {
            "grader_used": "llm_fallback",
            "evidence_source": "graph_nodes",
            "transcript_grader_failure": None,
            "score_before_dock": 0.0,
            "topics": [],
            "docks": [],
            "graph_lane": {"abstained": False, "reasons": ["normalization_confidence"]},
        },
    }
    write_artifacts_mock.assert_not_awaited()


async def test_flag_off_no_shadow_byte_identical(monkeypatch):
    """LIVE off, SHADOW off too ⇒ same golden, run_graph_simulation never
    called (mirrors WU-4C1's flag guard)."""
    out, shadow_mock, _write = await _run_with_flags(monkeypatch, shadow=False, live="false")
    shadow_mock.assert_not_awaited()
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"


async def test_live_on_healthy_not_abstained_promotes_graph_grade(monkeypatch):
    """(b) LIVE on + healthy shadow, not abstained ⇒ response["rubric"] IS the
    shadow's graph_sim_rubric object, and the canonical artifact is written
    with served="graph" (pair row llm)."""
    shadow = _shadow_result(abstained=False)
    out, shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        shadow_return=shadow,
    )
    shadow_mock.assert_awaited_once()
    assert out["rubric"] is shadow.graph_sim_rubric
    assert out["diagnostic_narrative"] == "graph-sim narrative"
    # coverage/progress/XP stay OLD-path.
    assert out["coverage"] == {}
    assert out["progress"]["xp_earned"] == 10

    write_artifacts_mock.assert_awaited_once()
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["served"] == GRADER_USED_GRAPH
    assert kwargs["shadow"] is shadow
    assert kwargs["graph_failure"] is None


async def test_live_on_exception_anywhere_falls_back_to_old_path(monkeypatch):
    """(c) LIVE on + run_graph_simulation raises ⇒ response equals the OLD-path
    values (HTTP 200 upstream, no re-raise), and the artifact records
    graph_failure containing the exception text."""
    out, shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        shadow_side_effect=RuntimeError("boom"),
    )
    shadow_mock.assert_awaited_once()
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"

    write_artifacts_mock.assert_awaited_once()
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["served"] == GRADER_USED_LLM_FALLBACK
    assert kwargs["shadow"] is None
    assert "boom" in kwargs["graph_failure"]
    assert "RuntimeError" in kwargs["graph_failure"]


async def test_live_off_exception_isolated_serves_llm_grade(monkeypatch):
    """Lane B1 / G3 — SHADOW mode (LIVE off) now ISOLATES a shadow-chain
    exception instead of re-raising it: pre-G3 the crash propagated and 500'd
    the Done request, costing the student the already-committed LLM grade. Now
    the OLD/LLM values are served (HTTP 200, no re-raise) and the canonical
    artifact records the shadow-failure marker so paired analysis sees the gap.
    (Full early/mid/late byte-identity coverage lives in
    ``test_done_shadow_isolation``.)"""
    out, shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="false",
        artifact=True,
        shadow_side_effect=RuntimeError("boom"),
    )
    shadow_mock.assert_awaited_once()
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"

    write_artifacts_mock.assert_awaited_once()
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["served"] == GRADER_USED_LLM_FALLBACK
    assert kwargs["shadow"] is None
    assert kwargs["graph_failure"].startswith(done_mod._SHADOW_FAILURE_MARKER)
    assert "boom" in kwargs["graph_failure"]


async def test_live_on_abstained_shadow_falls_back_to_llm(monkeypatch):
    """(d) LIVE on + abstained shadow ⇒ OLD/LLM values are served
    (grader_used stays "llm_fallback") without re-running anything at
    Done-time; the shadow result (carrying the real abstention reasons) is
    still forwarded to `write_artifacts` so the paired graph artifact records
    them (`build_graph_artifact` already reshapes
    `shadow.audited.abstention_reasons` verbatim — proven in
    `apollo/grading/tests/test_artifact_build.py`)."""
    shadow = _shadow_result(abstained=True)
    out, shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        shadow_return=shadow,
    )
    shadow_mock.assert_awaited_once()
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"

    write_artifacts_mock.assert_awaited_once()
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["served"] == GRADER_USED_LLM_FALLBACK
    assert kwargs["shadow"] is shadow
    assert list(shadow.audited.abstention_reasons) == ["normalization_confidence"]
    assert kwargs["graph_failure"] is None


async def test_scorecard_absent_when_artifact_flag_off(monkeypatch):
    """Task B1: no artifact write ⇒ nothing to template over ⇒ no scorecard
    key at all (not even ``None``)."""
    out, _shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=False,
        shadow_return=_shadow_result(),
    )
    write_artifacts_mock.assert_not_awaited()
    assert "scorecard" not in out


async def test_scorecard_attached_from_write_artifacts_return_value(monkeypatch):
    """Task B1: when artifact capture is on, ``handle_done`` renders the
    scorecard from EXACTLY the canonical payload ``write_artifacts`` returns
    — no recomputation, no second lookup."""
    out, _shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        shadow_return=_shadow_result(),
        write_artifacts_return=_FAKE_CANONICAL_PAYLOAD,
    )
    write_artifacts_mock.assert_awaited_once()
    assert out["scorecard"] == {
        "score_0_100": 90,
        "band": "Strong",
        "taught_well": [],
        "missing_or_unclear": [],
        "watch_out": [],
        # Lane B3a/D1 — no empty-bank marker on this seeded payload -> "checked".
        "watch_out_status": "checked",
        "watch_out_note": None,
        "clarifications": [],
    }


async def test_scorecard_absent_when_artifact_write_fails(monkeypatch):
    """Task B1: ``write_artifacts`` returning ``None`` (its own failure
    domain — see ``artifact_writer.write_artifacts``) means no scorecard is
    attached rather than templating over a payload that never made it to
    disk."""
    out, _shadow_mock, write_artifacts_mock = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        shadow_return=_shadow_result(),
        write_artifacts_return=None,
    )
    write_artifacts_mock.assert_awaited_once()
    assert "scorecard" not in out


async def test_old_rubric_still_forwarded_to_shadow(monkeypatch):
    """Unchanged plumbing: run_graph_simulation still gets old_rubric=<the OLD
    student-facing rubric> regardless of the A4 flag state."""
    _out, shadow_mock, _write = await _run_with_flags(
        monkeypatch,
        shadow=True,
        live="true",
        shadow_return=_shadow_result(),
    )
    kwargs = shadow_mock.await_args.kwargs
    assert kwargs["old_rubric"] == {"overall": {"score": 0.5}}


# ---------------------------------------------------------------------------
# Task B2 — mastery projection call site (`_project_mastery`), guarded
# mutually-exclusive with the dormant WU-5A2 Layer-3 belief path.
# ---------------------------------------------------------------------------


async def test_mastery_projection_runs_when_artifact_written_and_layer3_off(monkeypatch):
    """Artifact capture succeeded (`canonical_payload is not None`) and the
    Layer-3 flag is off (the only build state) ⇒ `_project_mastery` is
    awaited with this attempt's id."""
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    project_mock = AsyncMock(return_value=None)
    with patch("apollo.handlers.done._project_mastery", new=project_mock):
        out, _shadow_mock, write_artifacts_mock = await _run_with_flags(
            monkeypatch,
            shadow=False,
            live=None,
            artifact=True,
            write_artifacts_return=_FAKE_CANONICAL_PAYLOAD,
        )
    write_artifacts_mock.assert_awaited_once()
    project_mock.assert_awaited_once()
    assert project_mock.await_args.kwargs["attempt_id"] == 99
    assert "scorecard" in out


async def test_mastery_projection_skipped_when_layer3_active(monkeypatch):
    """Layer-3 (dormant WU-5A2 Bayesian belief path) is enabled ⇒
    `_project_mastery` must NEVER be called for the same attempt (both write
    `apollo_mastery_events`/`apollo_learner_state`; running both would
    double-apply evidence)."""
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "true")
    project_mock = AsyncMock(return_value=None)
    with patch("apollo.handlers.done._project_mastery", new=project_mock):
        out, _shadow_mock, write_artifacts_mock = await _run_with_flags(
            monkeypatch,
            shadow=False,
            live=None,
            artifact=True,
            write_artifacts_return=_FAKE_CANONICAL_PAYLOAD,
        )
    write_artifacts_mock.assert_awaited_once()
    project_mock.assert_not_awaited()
    # The scorecard (Task B1) is unaffected by the Layer-3 guard.
    assert "scorecard" in out


# ---------------------------------------------------------------------------
# Task B2 — `_project_mastery` itself: defensive no-op / success / own-
# failure-domain rollback, exercised directly against a mocked AsyncSession
# (real-PG entity-resolution + upsert behavior lives in
# `tests/database/test_artifact_mastery_postgres.py`).
# ---------------------------------------------------------------------------


def _db_returning(scalar_result):
    """A mocked AsyncSession whose `execute(...).scalar_one_or_none()`
    returns `scalar_result`."""
    db = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=scalar_result)
    db.execute = AsyncMock(return_value=execute_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


async def test_project_mastery_noop_when_no_canonical_row_found():
    """Defensive: `write_artifacts` already returned non-`None`, so a missing
    canonical row here is unreachable in practice — but if it ever happens,
    this must be a silent no-op (no projection call, no commit)."""
    db = _db_returning(None)
    with patch("apollo.handlers.done.update_mastery_from_artifact", new=AsyncMock()) as project:
        await _project_mastery(db, attempt_id=42)
    project.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_project_mastery_success_commits():
    row = MagicMock(name="GradingArtifact")
    db = _db_returning(row)
    with patch("apollo.handlers.done.update_mastery_from_artifact", new=AsyncMock()) as project:
        await _project_mastery(db, attempt_id=42)
    project.assert_awaited_once_with(db, artifact_row=row)
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


async def test_project_mastery_exception_rolls_back_and_is_swallowed():
    """Own-failure-domain posture (mirrors `write_artifacts`): ANY exception
    inside the projection is logged and swallowed — never raised into the
    Done response — and the session is rolled back."""
    row = MagicMock(name="GradingArtifact")
    db = _db_returning(row)
    with patch(
        "apollo.handlers.done.update_mastery_from_artifact",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await _project_mastery(db, attempt_id=42)  # must not raise
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
