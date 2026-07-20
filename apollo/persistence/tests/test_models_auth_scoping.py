"""Phase-1 auth retrofit: apollo models carry the house identity pattern."""

from __future__ import annotations

from apollo.persistence.models import StudentProgress, TutoringSession


def test_apollo_session_has_user_and_space_columns():
    cols = TutoringSession.__table__.columns
    assert "user_id" in cols and not cols["user_id"].nullable
    assert "search_space_id" in cols and not cols["search_space_id"].nullable
    assert "student_id" not in cols
    fks = {fk.target_fullname for fk in cols["search_space_id"].foreign_keys}
    assert "app.courses.id" in fks


def test_student_progress_keyed_by_user_and_course():
    cols = StudentProgress.__table__.columns
    assert cols["user_id"].primary_key
    assert cols["course_id"].primary_key
    assert "student_id" not in cols


def test_unique_active_index_uses_user_and_course():
    idx = {i.name: i for i in TutoringSession.__table__.indexes}
    active = idx["learning_activities__active_tutoring_user_course__uidx"]
    assert active.unique
    assert [c.name for c in active.columns] == ["user_id", "course_id"]
