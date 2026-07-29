from sqlalchemy import select, update

from apollo.persistence.models import (
    ProblemAttempt,
    StudentProgress,
    TutoringMessage,
    TutoringSession,
    promote_tutoring_message_metadata,
)


def test_tutoring_session_mapper_scopes_selects_and_updates_by_modality():
    selected = str(select(TutoringSession).compile(compile_kwargs={"literal_binds": True}))
    updated = str(
        update(TutoringSession)
        .where(TutoringSession.status == "active")
        .values(status="ended")
        .compile(compile_kwargs={"literal_binds": True})
    )

    assert TutoringSession.__table__.fullname == "app.learning_activities"
    assert TutoringSession.__mapper__.polymorphic_identity == "tutoring"
    assert "learning_activities.modality IN ('tutoring')" in selected
    assert "learning_activities.modality IN ('tutoring')" in updated


def test_tutoring_children_and_progress_use_target_physical_columns():
    assert TutoringMessage.__table__.fullname == "app.tutoring_messages"
    assert TutoringMessage.session_id.property.columns[0].name == "learning_activity_id"
    assert ProblemAttempt.__table__.fullname == "app.problem_attempts"
    assert ProblemAttempt.session_id.property.columns[0].name == "learning_activity_id"
    assert StudentProgress.__table__.fullname == "app.student_progress"
    assert tuple(StudentProgress.__table__.primary_key.columns.keys()) == (
        "user_id",
        "course_id",
    )


def test_tutoring_message_metadata_promotes_stable_signals():
    metadata, low_confidence_pattern, intent = promote_tutoring_message_metadata(
        {"low_conf_pattern": True, "intent": "done", "olm_invite": {"version": 1}}
    )

    assert metadata == {"olm_invite": {"version": 1}}
    assert low_confidence_pattern is True
    assert intent == "done"
