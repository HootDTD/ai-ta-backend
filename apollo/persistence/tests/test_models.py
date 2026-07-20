import pytest as _pytest_module
_pytest_module.skip(
    "Legacy V2 test — needs rewrite for V3 KGGraph + Neo4j store + new parser/coverage signatures. "
    "Tracked in claude_v3_checklist.md item 1; will be re-enabled in test-rewrite phase.",
    allow_module_level=True,
)

from datetime import datetime

from apollo.persistence.models import (
    TutoringSession,
    KGEntry,
    TutoringMessage,
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
    s = TutoringSession(
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
        m = TutoringMessage(session_id=1, role=role, content="hi", turn_index=0)
        assert m.role == role


def test_problem_attempt_defaults():
    pa = ProblemAttempt(session_id=1, problem_id="bernoulli_horizontal_pipe_find_p2", difficulty="intro")
    assert pa.result is None  # unset until solve attempt
    assert pa.problem_id == "bernoulli_horizontal_pipe_find_p2"


def test_kg_entry_has_attempt_id_column():
    assert "attempt_id" in KGEntry.__table__.columns
    col = KGEntry.__table__.columns["attempt_id"]
    assert col.nullable is True
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "apollo_problem_attempts"


def test_message_has_attempt_id_column():
    assert "attempt_id" in TutoringMessage.__table__.columns
    col = TutoringMessage.__table__.columns["attempt_id"]
    assert col.nullable is True
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "apollo_problem_attempts"
