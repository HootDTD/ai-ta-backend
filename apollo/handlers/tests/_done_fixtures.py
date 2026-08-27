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
        # INTERACTION1 column consumed by INTERACTION2 grounding; NULL is the
        # default state (no bundle built) and keeps the base golden ungrounded.
        self.grounding_bundle = None


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
    # M1b (P3.4 delta): `_grade_claimed_attempt`'s fence-loss branch calls
    # `db.rollback()` directly (not via a separately-mockable helper), so the
    # golden `db` double needs it awaitable even though the golden path never
    # takes that branch.
    db.rollback = AsyncMock()
    # Fix-round-1 (P3.4): the post-claim already-graded re-check calls
    # `db.refresh(attempt)` unconditionally on every successful claim. It's a
    # no-op against this plain `_Attempt` double (not a real ORM instance),
    # which is exactly right for the golden path — `attempt.result` stays
    # `None` either way.
    db.refresh = AsyncMock()
    patches = [
        patch("apollo.handlers.done._find_problem", new=AsyncMock(side_effect=_find_problem)),
        # Empty-attempt guard (defect I1): the base golden is a non-empty
        # attempt. The guard test overrides this with 0.
        patch("apollo.handlers.done._student_message_count", new=AsyncMock(return_value=1)),
        patch("apollo.handlers.done.KGStore.read_graph", new=AsyncMock(return_value=KGGraph())),
        # M1 (P3.4): the claim is a Core UPDATE against a real row, which the
        # MagicMock `db` above cannot serve — patch the seam, not the SQL. The
        # golden path is "this Done owns the claim" through to the terminal
        # fence too (M1b delta's `_fence_grade_commit`). Fix-round-2 reverted
        # the claim to a plain bool (a fencing-token stamp was tried and
        # reverted — see `_claim_grading_slot`'s docstring).
        patch("apollo.handlers.done._claim_grading_slot", new=AsyncMock(return_value=True)),
        patch("apollo.handlers.done._release_grading_claim", new=AsyncMock()),
        # Fix-round-1 (Minor #4): `set_committed_value` requires a real
        # SQLAlchemy-mapped instance (it reaches into `instance_state`), which
        # the plain `_Sess` double above is not. Stand in with the equivalent
        # observable effect for the golden path — real ORM instances are
        # exercised for real by the Docker-backed `tests/database/` gates.
        patch(
            "apollo.handlers.done.set_committed_value",
            new=lambda instance, key, value: setattr(instance, key, value),
        ),
        patch("apollo.handlers.done._fence_grade_commit", new=AsyncMock(return_value=True)),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        # Transcript grader is the sole (unconditional) grading lane.
        patch("apollo.handlers.done._full_transcript", new=AsyncMock(return_value=())),
        # Question ledger (P1.2b/P1.3): one read feeding the adjudicator's
        # tally context and the scorer's probed-node set. The base golden is an
        # empty ledger; test_done_question_ledger overrides it by attribute.
        patch("apollo.handlers.done._question_ledger", new=AsyncMock(return_value=())),
        patch(
            "apollo.handlers.done.compute_transcript_coverage_with_spans",
            new=AsyncMock(return_value=({}, {})),
        ),
        # Topic scoring now serves unconditionally when it computes; the base
        # golden neutralizes it (returns None) so `served_rubric is rubric`
        # (the mocked {"overall": {"score": 0.5}}). Tests that exercise real
        # topic scoring drop this patch by attribute (see test_done_topic_score).
        patch("apollo.handlers.done.compute_topic_score", new=MagicMock(return_value=None)),
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
        # Artifact write is unconditional now; return None so the base golden
        # attaches no scorecard / mastery projection.
        patch("apollo.handlers.done.write_artifacts", new=AsyncMock(return_value=None)),
    ]
    return db, sess, attempt, patches
