from datetime import datetime

from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)


def test_session_phase_enum_has_all_required_states():
    required = {"INIT", "TEACHING", "PROBLEM_REVEAL", "SOLVING", "REPORT", "BETWEEN"}
    actual = {p.name for p in SessionPhase}
    assert required.issubset(actual)


def test_session_status_enum():
    assert {"active", "paused", "ended"} == {s.value for s in SessionStatus}


def test_apollo_session_instantiation():
    s = ApolloSession(
        student_id="stu-1",
        concept_cluster_id="fluid_mechanics",
        status=SessionStatus.active,
        phase=SessionPhase.INIT,
    )
    assert s.student_id == "stu-1"
    assert s.concept_cluster_id == "fluid_mechanics"
    assert s.phase == SessionPhase.INIT


def test_kgentry_source_values_constrained_to_parser_or_student():
    # source is a Text column; values are enforced by the SQL CHECK
    # constraint (tested in migration). Python-side we just ensure the
    # default is 'parser'.
    e = KGEntry(
        session_id=1,
        type="equation",
        content={"symbolic": "A1*v1 - A2*v2", "label": "Continuity"},
    )
    assert e.source == "parser"


def test_message_roles():
    for role in ("student", "apollo", "system"):
        m = Message(session_id=1, role=role, content="hi", turn_index=0)
        assert m.role == role


def test_problem_attempt_defaults():
    pa = ProblemAttempt(session_id=1, problem_id="bernoulli_horizontal_pipe_find_p2", difficulty="intro")
    assert pa.result is None  # unset until solve attempt
    assert pa.problem_id == "bernoulli_horizontal_pipe_find_p2"
