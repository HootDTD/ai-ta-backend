"""WU-4C1 — the LOAD-BEARING flag guard for the Done shadow chain.

`handle_done`'s student-facing return MUST be byte-identical whether
``APOLLO_GRAPH_SIM_SHADOW_ENABLED`` is on or off. When off, the new
``run_graph_simulation`` chain must NEVER be called. When on, it is called once
with the OLD-path collaborators' inputs, but the returned dict is STILL the
OLD-path dict (the shadow result is not merged into the student response).

These are pure unit tests: every OLD-path collaborator is mocked
deterministically, Neo4j is a MagicMock, and ``run_graph_simulation`` is patched
on the ``done`` module so no real chain (and no live LLM) runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers import done as done_mod
from apollo.handlers.done import _graph_sim_shadow_enabled, handle_done
from apollo.ontology import KGGraph

pytestmark = pytest.mark.unit


_USER_ID = "a0000000-0000-4000-8000-000000000001"


class _Sess:
    """Minimal ApolloSession stand-in the OLD path reads."""

    def __init__(self) -> None:
        self.id = 11
        self.user_id = _USER_ID
        self.search_space_id = 7
        self.concept_id = 3
        self.current_problem_id = "p_code"
        self.phase = "TEACHING"


class _Attempt:
    def __init__(self) -> None:
        self.id = 99
        self.problem_id = "p_code"
        self.difficulty = "intro"
        self.result = None
        self.solver_trace = None
        self.diagnostic_report = None
        self.learner_update_pending = False


def _envelope() -> MagicMock:
    return MagicMock(
        xp_earned=10,
        xp_before=0,
        xp_after=10,
        level_before=1,
        level_after=1,
        level_up=False,
        title_after="Novice",
        level_progress_pct=0.1,
        xp_to_next_level=90,
    )


def _problem() -> MagicMock:
    problem = MagicMock()
    problem.id = "p_code"
    problem.problem_text = "text"
    problem.reference_solution = []
    problem.to_kg_graph.return_value = KGGraph()
    return problem


def _old_path_patches():
    """Patch every OLD-path collaborator deterministically + the DB read.

    Returns the list of context managers; the caller enters them. ``db`` /
    ``sess`` / ``attempt`` are wired through a fake ``db.execute`` so the OLD
    path resolves the session + attempt without a real database.
    """
    sess = _Sess()
    attempt = _Attempt()

    async def _find_problem(_db, _cid, _code):
        return _problem()

    # db.execute returns a result whose .scalar_one() -> sess and whose
    # .scalars().first() -> attempt, depending on call order. The OLD path
    # calls scalar_one() (session) first, then scalars().first() (attempt).
    db = MagicMock()

    class _SessResult:
        def scalar_one(self_inner):
            return sess

    class _AttemptResult:
        def scalars(self_inner):
            m = MagicMock()
            m.first.return_value = attempt
            return m

    calls = {"n": 0}

    async def _execute(*_a, **_kw):
        calls["n"] += 1
        return _SessResult() if calls["n"] == 1 else _AttemptResult()

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    patches = [
        patch("apollo.handlers.done._find_problem", new=AsyncMock(side_effect=_find_problem)),
        patch("apollo.handlers.done.KGStore.read_graph", new=AsyncMock(return_value=KGGraph())),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        patch("apollo.handlers.done.compute_coverage", new=AsyncMock(return_value={})),
        patch("apollo.handlers.done._attempt_misconception_scores", new=AsyncMock(return_value={})),
        patch("apollo.handlers.done.compute_rubric", return_value={"overall": {"score": 0.5}}),
        patch("apollo.handlers.done.generate_diagnostic", return_value="narrative"),
        patch("apollo.handlers.done.has_prior_graded_attempt", new=AsyncMock(return_value=False)),
        patch("apollo.handlers.done.compute_xp_earned", return_value=10),
        patch("apollo.handlers.done.apply_xp", new=AsyncMock(return_value={"xp_before": 0, "xp_after": 10})),
        patch("apollo.handlers.done.compute_progress_envelope", return_value=_envelope()),
    ]
    return db, sess, attempt, patches


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    yield


async def test_shadow_flag_off_return_byte_identical(monkeypatch):
    """THE single most important test: flag OFF -> run_graph_simulation is NEVER
    called and the returned dict is the OLD-path dict, unchanged."""
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    db, _sess, _attempt, patches = _old_path_patches()

    shadow = MagicMock()
    with patch("apollo.handlers.done.run_graph_simulation", new=shadow):
        for p in patches:
            p.start()
        try:
            out = await handle_done(db=db, neo=MagicMock(), session_id=11)
        finally:
            for p in reversed(patches):
                p.stop()

    shadow.assert_not_called()
    # The frozen golden student-facing payload (OLD-path values ONLY).
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"
    assert out["coverage"] == {}
    assert out["xp_earned"] == 10
    assert out["xp_before"] == 0
    assert out["xp_after"] == 10
    assert out["level_up"] is False
    assert out["progress"]["xp_earned"] == 10
    assert out["progress"]["title_after"] == "Novice"


async def test_shadow_flag_on_invokes_chain(monkeypatch):
    """Flag ON -> run_graph_simulation IS called once with the OLD-path
    student_graph/attempt/sess/problem_payload, and the returned dict is STILL
    the OLD-path dict (chain result not merged into the student response)."""
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    db, sess, attempt, patches = _old_path_patches()

    sentinel = MagicMock(name="ShadowGradeResult")
    shadow = AsyncMock(return_value=sentinel)
    payload = {"declared_paths": [["a"]], "symbolic_mappings": {"d": "2*r"}}

    with (
        patch("apollo.handlers.done.run_graph_simulation", new=shadow),
        patch("apollo.handlers.done._find_problem_payload", new=AsyncMock(return_value=payload)),
    ):
        for p in patches:
            p.start()
        try:
            out = await handle_done(db=db, neo=MagicMock(), session_id=11)
        finally:
            for p in reversed(patches):
                p.stop()

    shadow.assert_awaited_once()
    kwargs = shadow.await_args.kwargs
    assert kwargs["attempt"] is attempt
    assert kwargs["sess"] is sess
    assert kwargs["problem_payload"] is payload
    # the pre-freeze graph object is the student_graph handed to the chain
    assert isinstance(kwargs["student_graph"], KGGraph)
    # student-facing dict is the OLD-path dict, NOT the chain sentinel
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out is not sentinel


def test_shadow_flag_parsing(monkeypatch):
    for truthy in ("1", "true", "TRUE", "Yes", "yes"):
        monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", truthy)
        assert _graph_sim_shadow_enabled() is True
    for falsy in ("0", "false", "no", "", "off", "maybe"):
        monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", falsy)
        assert _graph_sim_shadow_enabled() is False
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    assert _graph_sim_shadow_enabled() is False


def test_flag_constant_name():
    """Pin the env-var name so prod/test config keys match the spec."""
    assert done_mod._GRAPH_SIM_SHADOW_FLAG == "APOLLO_GRAPH_SIM_SHADOW_ENABLED"
