"""Campaign-plan Task B3 — router-level wiring for the teacher classroom
endpoints (``GET /apollo/teacher/classroom/{search_space_id}/heatmap`` and
``.../struggles``).

Follows the direct-call pattern already used by
``apollo/provisioning/tests/test_authored_api.py``: call the route function
directly against a real-PG ``db_session``, monkeypatching
``require_user``/``require_course_teacher`` on ``apollo.api`` so no live
Supabase token is needed. The aggregation SQL itself (jsonb lateral
expansion, windowing, scoping) is proven separately in
``tests/database/test_classroom_projection_postgres.py``; these tests prove
the routes are wired to the right functions and gated by the right auth
dependency.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import apollo.api as apollo_api
from apollo.persistence.models import KGEntity, LearnerState
from apollo.subjects.tests._curriculum_fixtures import seed_concept, seed_search_space
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


async def _seed_scope(db) -> tuple[int, int]:
    sid = await seed_search_space(db)
    cid = await seed_concept(
        db, search_space_id=sid, subject_slug=f"subj-{uuid.uuid4().hex[:8]}", concept_slug="c1"
    )
    return sid, cid


async def test_heatmap_route_requires_teacher(db_session, monkeypatch):
    sid, _cid = await _seed_scope(db_session)
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher_forbidden)

    with pytest.raises(HTTPException) as exc:
        await apollo_api.classroom_heatmap(
            search_space_id=sid, request=_FakeRequest(), db=db_session,
        )
    assert exc.value.status_code == 403


async def test_heatmap_route_returns_rows(db_session, monkeypatch):
    sid, cid = await _seed_scope(db_session)
    entity = KGEntity(concept_id=cid, canonical_key="eq.a", kind="equation", display_name="eq.a")
    db_session.add(entity)
    await db_session.flush()
    db_session.add(
        LearnerState(
            user_id=str(uuid.uuid4()),
            search_space_id=sid,
            entity_id=entity.id,
            belief=[0.2, 0.0, 0.8],
            mastery=0.8,
            confidence=0.9,
            evidence_count=1,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher)

    out = await apollo_api.classroom_heatmap(
        search_space_id=sid, request=_FakeRequest(), db=db_session,
    )
    assert out["rows"] == [
        {
            "user_id": out["rows"][0]["user_id"],
            "concept_id": cid,
            "mastery": pytest.approx(0.8),
            "confidence": pytest.approx(0.9),
        }
    ]


async def test_struggles_route_requires_teacher(db_session, monkeypatch):
    sid, _cid = await _seed_scope(db_session)
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher_forbidden)

    with pytest.raises(HTTPException) as exc:
        await apollo_api.classroom_struggles(
            search_space_id=sid, request=_FakeRequest(), db=db_session,
        )
    assert exc.value.status_code == 403


async def test_struggles_route_returns_signals_shape(db_session, monkeypatch):
    sid, _cid = await _seed_scope(db_session)
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher)

    out = await apollo_api.classroom_struggles(
        search_space_id=sid, request=_FakeRequest(), db=db_session,
    )
    assert out == {
        "abstention_count": 0,
        "fallback_count": 0,
        "lowest_coverage_nodes": [],
        "top_misconceptions": [],
    }


async def test_struggles_route_honors_window_days_param(db_session, monkeypatch):
    sid, _cid = await _seed_scope(db_session)
    monkeypatch.setattr(apollo_api, "require_user", _fake_require_user)
    monkeypatch.setattr(apollo_api, "require_course_teacher", _fake_require_teacher)

    out = await apollo_api.classroom_struggles(
        search_space_id=sid, request=_FakeRequest(), window_days=1, db=db_session,
    )
    assert out["abstention_count"] == 0
