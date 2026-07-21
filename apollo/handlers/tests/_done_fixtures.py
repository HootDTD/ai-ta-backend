"""Deterministic collaborators for Done-handler unit tests."""

from unittest.mock import AsyncMock, MagicMock, patch

from apollo.ontology import KGGraph

_USER_ID = "a0000000-0000-4000-8000-000000000001"


class _Sess:
    def __init__(self) -> None:
        self.id = 11
        self.user_id = _USER_ID
        self.course_id = 7
        self.search_space_id = self.course_id
        self.concept_id = 3
        self.current_problem_id = 42
        self.phase = "TEACHING"


class _Attempt:
    def __init__(self) -> None:
        self.id = 99
        self.problem_id = 42
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
    problem.database_id = 42
    problem.problem_text = "text"
    problem.reference_solution = []
    problem.to_kg_graph.return_value = KGGraph()
    return problem


def _old_path_patches():
    sess = _Sess()
    attempt = _Attempt()

    async def _find_problem(_db, _cid, _code, *, course_id):
        assert course_id == sess.course_id
        return _problem()

    db = MagicMock()

    class _SessResult:
        def scalar_one(self):
            return sess

    class _AttemptResult:
        def scalars(self):
            result = MagicMock()
            result.first.return_value = attempt
            return result

    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
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
        patch(
            "apollo.handlers.done.apply_xp",
            new=AsyncMock(return_value={"xp_before": 0, "xp_after": 10}),
        ),
        patch("apollo.handlers.done.compute_progress_envelope", return_value=_envelope()),
        patch("apollo.handlers.done._fetch_attempt_transcript", new=AsyncMock(return_value=[])),
    ]
    return db, sess, attempt, patches
