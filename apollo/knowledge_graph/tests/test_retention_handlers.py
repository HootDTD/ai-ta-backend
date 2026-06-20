"""WU-3C1 — retention behavior of the lifecycle handlers (§7).

These assert the store-interaction CONTRACT of the handlers:
  * handle_end PERSISTS subgraphs (never calls delete_subgraph);
  * done.py stamps graded_at as the final post-commit retention write;
  * restart_problem STILL deletes (the one explicit student wipe).

No live infra: a lightweight async Postgres-session stub returns the session /
attempt rows, and KGStore methods are patched as AsyncMock spies. The handler
modules are imported and called directly. Test attempt_ids are NEGATIVE.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from apollo.handlers.done import handle_done
from apollo.handlers.lifecycle import handle_end
from apollo.handlers.restart_problem import handle_restart_problem
from apollo.persistence.models import SessionPhase, SessionStatus

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _Sess:
    """Mutable session row stand-in."""

    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", -1)
        self.user_id = kw.get("user_id", "u-1")
        self.search_space_id = kw.get("search_space_id", 1)
        self.status = kw.get("status", SessionStatus.active.value)
        self.phase = kw.get("phase", SessionPhase.TEACHING.value)
        self.current_problem_id = kw.get("current_problem_id", "p1")
        # WU-3D: the concept signal is the int concept_id FK (cluster string gone).
        self.concept_id = kw.get("concept_id", 1)


class _Attempt:
    def __init__(self, attempt_id: int) -> None:
        self.id = attempt_id
        self.result = None
        self.difficulty = "intro"
        self.solver_trace = None
        self.diagnostic_report = None


class _ScalarResult:
    def __init__(
        self, *, one: Any = None, first: Any = None, all_: list[Any] | None = None
    ) -> None:
        self._one = one
        self._first = first
        self._all = all_ or []

    def scalar_one(self) -> Any:
        return self._one

    def scalars(self) -> _ScalarResult:
        return self

    def first(self) -> Any:
        return self._first

    def all(self) -> list[Any]:
        return self._all


class _StubDB:
    """Returns queued results in order; records commit() calls."""

    def __init__(self, results: list[_ScalarResult]) -> None:
        self._results = list(results)
        self.commits = 0
        self.events: list[str] = []

    async def execute(self, *a: Any, **kw: Any) -> _ScalarResult:
        return self._results.pop(0) if self._results else _ScalarResult()

    async def commit(self) -> None:
        self.commits += 1
        self.events.append("commit")


# ---------------------------------------------------------------------------
# handle_end — does NOT delete (persists)
# ---------------------------------------------------------------------------


async def test_handle_end_does_not_delete_subgraph():
    sess = _Sess(id=-10, status=SessionStatus.active.value)
    # The session HAS per-attempt rows — pre-WU-3C1 handle_end queried them and
    # deleted each subgraph. WU-3C1 must NOT, even when attempts exist. The
    # second result (attempt ids) is queued so the OLD loop would have fired
    # delete_subgraph(-901)/(-902); the new handler never consumes it.
    db = _StubDB(
        [
            _ScalarResult(one=sess),
            _ScalarResult(all_=[-901, -902]),
        ]
    )
    neo = object()  # never touched — delete_subgraph is spied off

    with patch("apollo.handlers.lifecycle.KGStore.delete_subgraph", new_callable=AsyncMock) as spy:
        out = await handle_end(db=db, neo=neo, session_id=-10)

    spy.assert_not_awaited()
    assert out == {"ok": True}
    assert sess.status == SessionStatus.ended.value
    assert db.commits == 1  # the single 'mark ended' commit


# ---------------------------------------------------------------------------
# done.py — stamps graded_at as the LAST step, post-commit
# ---------------------------------------------------------------------------


async def test_done_stamps_graded_at():
    sess = _Sess(id=-20, current_problem_id="p1", concept_id=1)
    attempt = _Attempt(-201)

    db = _StubDB(
        [
            _ScalarResult(one=sess),  # load session
            _ScalarResult(first=attempt),  # load attempt
        ]
    )
    neo = object()

    order: list[str] = []

    async def _rec_commit() -> None:
        order.append("commit")

    db.commit = _rec_commit  # type: ignore[assignment]

    stamp_spy = AsyncMock(side_effect=lambda **kw: order.append("stamp"))
    fake_problem = _FakeProblem()

    with (
        patch("apollo.handlers.done._find_problem", new=AsyncMock(return_value=fake_problem)),
        patch("apollo.handlers.done.KGStore.read_graph", new_callable=AsyncMock) as read_graph,
        patch("apollo.handlers.done.KGStore.freeze", new_callable=AsyncMock),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", stamp_spy),
        patch("apollo.handlers.done.compute_coverage", new_callable=AsyncMock) as cov,
        patch("apollo.handlers.done._attempt_misconception_scores", new_callable=AsyncMock) as misc,
        patch("apollo.handlers.done.compute_rubric") as rubric,
        patch("apollo.handlers.done.generate_diagnostic", return_value="narrative"),
        patch("apollo.handlers.done.has_prior_graded_attempt", new_callable=AsyncMock) as prior,
        patch("apollo.handlers.done.compute_xp_earned", return_value=10),
        patch("apollo.handlers.done.apply_xp", new_callable=AsyncMock) as apply_xp_mock,
        patch("apollo.handlers.done.compute_progress_envelope") as envelope,
    ):
        read_graph.return_value = _FakeGraph()
        cov.return_value = {}
        misc.return_value = {}
        rubric.return_value = {"overall": {"score": 0.5}}
        prior.return_value = False
        apply_xp_mock.return_value = {"xp_before": 0, "xp_after": 10}
        envelope.return_value = _FakeEnvelope()

        await handle_done(db=db, neo=neo, session_id=-20)

    stamp_spy.assert_awaited_once_with(attempt_id=attempt.id)
    # stamp runs AFTER the grade commits (post-commit retention write).
    assert "stamp" in order
    assert order.index("stamp") > order.index("commit")


class _FakeGraph:
    nodes: list[Any] = []

    def model_dump(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return {"nodes": [], "edges": []}


class _FakeProblem:
    id = "p1"
    problem_text = "x"
    reference_solution: list[Any] = []

    def to_kg_graph(self, *, attempt_id: int) -> _FakeGraph:
        return _FakeGraph()


class _FakeEnvelope:
    xp_earned = 10
    xp_before = 0
    xp_after = 10
    level_before = 1
    level_after = 1
    level_up = False
    title_after = "Novice"
    level_progress_pct = 0.1
    xp_to_next_level = 90


# ---------------------------------------------------------------------------
# restart_problem — STILL deletes (regression pin)
# ---------------------------------------------------------------------------


async def test_restart_problem_still_deletes_subgraph():
    sess = _Sess(
        id=-30,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id="p1",
    )
    attempt = _Attempt(-301)
    db = _StubDB(
        [
            _ScalarResult(one=sess),  # session (with_for_update)
            _ScalarResult(first=attempt),  # current attempt
            _ScalarResult(),  # delete(Message)
        ]
    )
    neo = object()

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new_callable=AsyncMock,
    ) as spy:
        out = await handle_restart_problem(db=db, neo=neo, session_id=-30)

    spy.assert_awaited_once_with(attempt_id=attempt.id)
    assert out == {"ok": True}
    assert sess.phase == SessionPhase.TEACHING.value
