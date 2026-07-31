"""Router-level wiring for ``GET
/apollo/teacher/classroom/{search_space_id}/performance``.

Same direct-call pattern as ``test_classroom_routes.py``: call the route
function against a real-PG ``db_session``, monkeypatching
``require_user``/``require_course_teacher`` on ``apollo.api`` so no live
Supabase token is needed. The aggregation itself is proven in
``tests/database/test_class_performance_postgres.py``; these tests prove the
route is wired to ``class_performance`` and gated by the teacher dependency.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import apollo.api as apollo_api
from apollo.subjects.tests._curriculum_fixtures import seed_search_space
from auth import AuthContext

pytestmark = pytest.mark.integration


class _FakeRequest:
    pass


async def _fake_require_user(_request):
    return AuthContext(user_id="teacher-1", access_token="token")


async def _fake_require_teacher(**_kwargs):
    return None


async def _fake_require_teacher_forbidden(**_kwargs):
    raise HTTPException(status_code=403, detail="Forbidden for this course")


async def test_performance_route_requires_teacher(db_session, monkeypatch):
    sid = await seed_search_space(db_session)
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher_forbidden)

    with pytest.raises(HTTPException) as exc:
        await apollo_api.classroom_performance(
            search_space_id=sid,
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 403


async def test_performance_route_returns_payload(db_session, monkeypatch):
    sid = await seed_search_space(db_session)
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher)

    payload = await apollo_api.classroom_performance(
        search_space_id=sid,
        request=_FakeRequest(),
        db=db_session,
    )
    assert set(payload) == {
        "roster",
        "totals",
        "class_average",
        "grade_distribution",
        "activity_by_day",
        "rubric_averages",
        "concepts",
        "problems",
        "students",
        "insights",
    }
    assert payload["students"] == []
    assert payload["problems"] == []
    assert payload["insights"] == {
        "correlation": None,
        "effort_quartiles": None,
        "retry_payoff": None,
    }


async def test_performance_route_scopes_to_course(db_session, monkeypatch):
    """A course id with no rows at all yields the zeroed payload — the route
    passes search_space_id through to the projection unchanged."""
    sid = await seed_search_space(db_session)
    other = sid + int(uuid.uuid4().int % 1000) + 1
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher)

    payload = await apollo_api.classroom_performance(
        search_space_id=other,
        request=_FakeRequest(),
        db=db_session,
    )
    assert payload["roster"] == {"students": 0, "teachers": 0}
    assert payload["totals"]["attempts"] == 0
