"""Subject-fluid Apollo Stage-6 (Goal C) — teach-back runs subject-fluidly.

Proves AC #6: a student teaches back BOTH a fluid and an argument (polisci)
reference graph, the grader returns a ``GradeResult``, and the learner model
(``apollo_learner_state`` / ``apollo_mastery_events``) updates — through the SAME
frozen ``grade_attempt`` + ``persist_learner_update`` path, with NO subject-specific
branch. NO Neo4j / NO LLM: the student/reference graphs are hand-built (the
WU-4A2 ``_builders``) and the ``ShadowGradeResult`` + entity map are seeded via ORM
(the WU-5A2 ``test_done_layer3_route_postgres`` pattern).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from apollo.conftest import TEST_USER_ID
from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.core import GradeResult, grade_attempt
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.graph_compare.tests._builders import cnode, path, rgraph, rnode, snorm
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.learner_model.persistence import persist_learner_update
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    KGEntity,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    Subject,
)
from database.models import SearchSpace

# pytest.ini sets asyncio_mode = auto.


# --------------------------------------------------------------------------- #
# Part A — the grader (grade_attempt) runs on an ARGUMENT graph (pure)
# --------------------------------------------------------------------------- #


def _argument_reference():
    """A prose ARGUMENT reference graph in the qualitative node vocab
    (definition / condition / procedure_step), one declared path covering it."""
    return rgraph(
        nodes=(
            rnode("def.federalism", "definition"),
            rnode("cond.dispersed_power", "condition"),
            rnode("proc.veto_points", "procedure_step"),
            rnode("proc.conclusion", "procedure_step"),
        ),
        paths=(
            path("def.federalism", "cond.dispersed_power", "proc.veto_points", "proc.conclusion"),
        ),
    )


def test_grade_attempt_full_teachback_on_argument_graph():
    """A student who teaches back EVERY argument node scores full coverage — the
    grader is subject-fluid (handles definition/condition/procedure_step typed
    nodes with no change)."""
    student = snorm(
        nodes=(
            cnode("def.federalism", "definition"),
            cnode("cond.dispersed_power", "condition"),
            cnode("proc.veto_points", "procedure_step"),
            cnode("proc.conclusion", "procedure_step"),
        )
    )
    result = grade_attempt(student, _argument_reference())
    assert isinstance(result, GradeResult)
    assert result.coverage_score == 1.0
    covered_keys = {f.canonical_key for f in result.findings if f.kind == FindingKind.COVERED_NODE}
    assert "def.federalism" in covered_keys
    assert "proc.conclusion" in covered_keys


def test_grade_attempt_partial_teachback_on_argument_graph():
    """A student who omits the conclusion gets partial coverage + a missing
    finding — the same grading semantics as a fluid graph."""
    student = snorm(
        nodes=(
            cnode("def.federalism", "definition"),
            cnode("cond.dispersed_power", "condition"),
            cnode("proc.veto_points", "procedure_step"),
        )
    )
    result = grade_attempt(student, _argument_reference())
    assert 0.0 < result.coverage_score < 1.0
    missing = {f.canonical_key for f in result.findings if f.kind == FindingKind.MISSING_NODE}
    assert "proc.conclusion" in missing


# --------------------------------------------------------------------------- #
# Part B — the learner model updates for BOTH a fluid and an argument entity
# --------------------------------------------------------------------------- #


async def _seed_course_entity_session(
    db, *, course_slug: str, canonical_key: str, kind: str, user_id=TEST_USER_ID
):
    """Seed course/subject/concept + ONE KGEntity (of the given kind) + a
    session/attempt. Returns (entity_id, sess, attempt). ``user_id`` is a param so
    two teach-backs in one db_session don't trip the one-active-session-per-user
    unique index."""
    space = SearchSpace(name=course_slug, slug=course_slug, subject_name="X")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s_{course_slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    concept = Concept(subject_id=subj.id, slug=f"k_{course_slug}", display_name="C")
    db.add(concept)
    await db.flush()
    ent = KGEntity(
        concept_id=concept.id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=canonical_key,
        payload={},
        aliases=[],
    )
    db.add(ent)
    await db.flush()
    sess = ApolloSession(
        user_id=user_id,
        search_space_id=space.id,
        concept_id=concept.id,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id=f"p_{course_slug}",
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id, problem_id=f"p_{course_slug}", difficulty="standard", result="graded"
    )
    db.add(attempt)
    await db.flush()
    return ent.id, sess, attempt


def _covered_finding(key: str):
    return Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key=key,
        student_node_ids=("stu_1",),
        score=1.0,
        confidence=0.9,
    )


def _shadow_for(key: str) -> ShadowGradeResult:
    audited = AuditedGrade(
        grade=object(),
        findings=(_covered_finding(key),),
        abstention_reasons=(),
        abstained=False,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    return ShadowGradeResult(
        run_id=1,
        grade=object(),
        audited=audited,
        normalization_confidence=0.8,
        reference_graph_hash="refhash-v1:beef",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),
        diagnostic=object(),
    )


async def _teach_back_one(
    db, *, course_slug: str, canonical_key: str, kind: str, user_id=TEST_USER_ID
):
    """Drive one teach-back's learner update end-to-end and return (result, n_events,
    n_states) read back from the DB."""
    entity_id, sess, attempt = await _seed_course_entity_session(
        db, course_slug=course_slug, canonical_key=canonical_key, kind=kind, user_id=user_id
    )
    result = await persist_learner_update(
        db,
        sess=sess,
        attempt=attempt,
        shadow=_shadow_for(canonical_key),
        done_ts=datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
        parser_confidence=0.95,
        canon_key_by_canonical_key={canonical_key: entity_id},
    )
    await db.commit()
    n_events = (
        (await db.execute(select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)))
        .scalars()
        .all()
    )
    n_states = (
        (await db.execute(select(LearnerState).where(LearnerState.entity_id == entity_id)))
        .scalars()
        .all()
    )
    return result, n_events, n_states


async def test_learner_model_updates_for_fluid_teachback(db_session):
    """Back-compat: teaching back a fluid (equation) entity writes one mastery
    event + one learner_state."""
    result, events, states = await _teach_back_one(
        db_session, course_slug="tb-fluid", canonical_key="eq.continuity", kind="equation"
    )
    assert result.events_written == 1
    assert result.states_upserted == 1
    assert result.abstained is False
    assert len(events) == 1
    assert len(states) == 1
    assert len(states[0].belief) == 3  # a posterior belief vector was written


async def test_learner_model_updates_for_argument_teachback(db_session):
    """Subject-fluid (the new path): teaching back an ARGUMENT (procedure) entity
    writes one mastery event + one learner_state through the SAME frozen learner
    model — no subject-specific branch."""
    result, events, states = await _teach_back_one(
        db_session, course_slug="tb-poli", canonical_key="proc.veto_points", kind="procedure"
    )
    assert result.events_written == 1
    assert result.states_upserted == 1
    assert result.abstained is False
    assert len(events) == 1
    assert len(states) == 1
    assert len(states[0].belief) == 3


async def test_learner_model_argument_and_fluid_both_update_in_one_session(db_session):
    """Both subjects update in the SAME run — the learner model is subject-fluid
    (AC #6: 'updates for both')."""
    import uuid

    _, fluid_events, fluid_states = await _teach_back_one(
        db_session,
        course_slug="tb-both-f",
        canonical_key="eq.bernoulli",
        kind="equation",
        user_id=uuid.UUID("a0000000-0000-4000-8000-0000000000f1"),
    )
    _, arg_events, arg_states = await _teach_back_one(
        db_session,
        course_slug="tb-both-a",
        canonical_key="def.federalism",
        kind="definition",
        user_id=uuid.UUID("a0000000-0000-4000-8000-0000000000a2"),
    )
    assert len(fluid_events) == 1 and len(fluid_states) == 1
    assert len(arg_events) == 1 and len(arg_states) == 1
