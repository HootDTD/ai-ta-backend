"""WU-5A2 — real-PG acceptance gate: the all-or-nothing Layer-3 belief write.

Drives ``persist_learner_update`` / ``run_learner_update`` (and, for the
done_ts-identity + flag-off cases, the full ``handle_done`` route) against the
real ``db_session`` (Base.metadata.create_all, savepoint rollback). The REAL
Postgres CHECKs (belief length 3, score/mastery/confidence 0..1) + the UNIQUE
NULLS NOT DISTINCT + the SELECT FOR UPDATE + forced-mid-write ROLLBACK all run on
PG — NOT SQLite (models.py:45 — the SQLite variant has NO CHECKs).

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate.
No Neo4j, no LLM on 5A2's path: the ``ShadowGradeResult`` is hand-built and the
entity map is seeded directly via ORM (the ``test_canon_projection.py`` pattern).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from apollo.conftest import TEST_USER_ID
from apollo.grading.audited_grade import AuditedGrade
from apollo.grading.events import convert_findings_to_events
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done import handle_done
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.handlers.learner_update import run_learner_update
from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    bayes_update,
    damp,
    likelihood_for_event,
    mastery_of,
)
from apollo.learner_model.decay import decay_toward_prior
from apollo.learner_model.negotiation import NEGOTIATION_LIKELIHOOD
from apollo.learner_model.persistence import (
    LearnerUpdateResult,
    persist_learner_update,
)
from apollo.learner_model.update import apply_event
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    KGEntity,
    KGNegotiation,
    LearnerState,
    MasteryEvent,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    Subject,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_course,
)
from database.models import SearchSpace

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]

# The reference key the events are keyed on (matches the shadow harness'
# adjudicator stub mapping stu_continuity -> eq.continuity).
_CONTINUITY = "eq.continuity"


# ---------------------------------------------------------------------------
# Direct-ORM seed helpers (real Postgres via db_session)
# ---------------------------------------------------------------------------


async def _seed_course_with_entity(
    db,
    *,
    canonical_keys=(_CONTINUITY,),
    course_slug="c5a2",
):
    """Seed course/subject/concept + one KGEntity per canonical_key. Returns
    (search_space_id, concept_id, {canonical_key: entity_id})."""
    space = SearchSpace(name=course_slug, slug=course_slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s_{course_slug}", display_name="Fluids", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    concept = Concept(subject_id=subj.id, slug=f"k_{course_slug}", display_name="Continuity")
    db.add(concept)
    await db.flush()

    by_key: dict[str, int] = {}
    for ck in canonical_keys:
        ent = KGEntity(
            concept_id=concept.id,
            canonical_key=ck,
            kind="equation",
            display_name=ck,
            payload={},
            aliases=[],
        )
        db.add(ent)
        await db.flush()
        by_key[ck] = ent.id
    return space.id, concept.id, by_key


async def _seed_session_attempt(db, *, sid, cid, problem_code="p_layer3"):
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id=problem_code,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id, problem_id=problem_code, difficulty="intro", result="graded"
    )
    db.add(attempt)
    await db.flush()
    return sess, attempt


def _covered_finding(key=_CONTINUITY, *, score=1.0, confidence=0.9):
    return Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key=key,
        student_node_ids=("stu_1",),
        score=score,
        confidence=confidence,
    )


def _misconception_finding(key="misc.density_ignored"):
    return Finding(
        kind=FindingKind.CONTRADICTION,
        canonical_key=key,
        student_node_ids=("stu_m",),
        score=0.0,
    )


def _audited(findings, *, abstained=False, suppressed=frozenset()):
    return AuditedGrade(
        grade=object(),  # not read on the Layer-3 path
        findings=tuple(findings),
        abstention_reasons=(),
        abstained=abstained,
        suppressed_event_kinds=suppressed,
        alias_candidates=(),
    )


def _shadow(audited, *, normalization_confidence=0.8):
    """A minimal ShadowGradeResult: persist_learner_update reads only `.audited`,
    `.opposes_map`, `.turn_order`, `.normalization_confidence`."""
    return ShadowGradeResult(
        run_id=1,
        grade=object(),
        audited=audited,
        normalization_confidence=normalization_confidence,
        reference_graph_hash="refhash-v1:deadbeef",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),
        diagnostic=object(),
    )


def _belief_close(persisted, expected) -> bool:
    """REAL[] is single-precision Postgres float — compare at REAL tolerance, not
    double-precision identity."""
    return list(persisted) == pytest.approx(list(expected), rel=1e-5, abs=1e-6)


def _expected_update(event, *, parser_confidence, normalization_confidence, prior_belief=None,
                     prior_last_evidence_at=None, done_ts):
    """Recompute the WU-5A1 pure result so the test asserts against the math
    core, not a hard-coded float."""
    return apply_event(
        event,
        prior_belief=prior_belief,
        prior_last_evidence_at=prior_last_evidence_at,
        parser_confidence=parser_confidence,
        grader_confidence=normalization_confidence * 1.0,
        done_ts=done_ts,
    )


# ---------------------------------------------------------------------------
# 2. persist_learner_update happy path (contracts 2,7,11)
# ---------------------------------------------------------------------------


async def test_layer3_persists_events_and_state(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    finding = _covered_finding(score=1.0)
    audited = _audited([finding])
    shadow = _shadow(audited, normalization_confidence=0.8)

    result = await persist_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=shadow,
        done_ts=done_ts,
        parser_confidence=0.95,
        canon_key_by_canonical_key=by_key,
    )
    await db_session.commit()

    assert result.events_written == 1
    assert result.states_upserted == 1
    assert result.skipped_unmapped == ()
    assert result.abstained is False

    # exactly one mastery event + one learner_state for this entity
    events = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalars().all()
    assert len(events) == 1
    me = events[0]
    assert me.entity_id == by_key[_CONTINUITY]
    assert me.event_kind == "covered"

    state = (await db_session.execute(
        select(LearnerState).where(
            LearnerState.user_id == TEST_USER_ID,
            LearnerState.entity_id == by_key[_CONTINUITY],
        )
    )).scalar_one()

    # the persisted posterior matches the WU-5A1 pure core (cold-start base)
    event = convert_findings_to_events(audited, opposes_map={}, turn_order={})[0]
    expected = _expected_update(
        event, parser_confidence=0.95, normalization_confidence=0.8, done_ts=done_ts
    )
    assert _belief_close(state.belief, expected.posterior_belief)
    assert round(state.mastery, 9) == round(expected.mastery_after, 9)
    assert state.evidence_count == 1
    assert state.last_evidence_at == done_ts
    # the event row's grader_confidence is normalization_confidence * 1.0
    assert round(me.grader_confidence, 6) == 0.8
    assert round(me.parser_confidence, 6) == 0.95


# ---------------------------------------------------------------------------
# 3. unmapped canonical_key -> SKIP (contract 6)
# ---------------------------------------------------------------------------


async def test_unmapped_canonical_key_skipped(db_session):
    # entity map is EMPTY -> the covered event's key is unmapped -> skipped.
    sid, cid, _ = await _seed_course_with_entity(db_session, canonical_keys=())
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    audited = _audited([_covered_finding()])
    result = await persist_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(audited),
        done_ts=done_ts,
        parser_confidence=0.9,
        canon_key_by_canonical_key={},  # empty pre-promotion specs
    )
    await db_session.commit()

    assert result.events_written == 0
    assert result.states_upserted == 0
    assert result.skipped_unmapped == (_CONTINUITY,)
    assert result.abstained is False  # events existed, just unmapped

    me = (await db_session.execute(
        select(func.count()).select_from(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    ls = (await db_session.execute(
        select(func.count()).select_from(LearnerState).where(LearnerState.user_id == TEST_USER_ID)
    )).scalar_one()
    assert me == 0
    assert ls == 0


# ---------------------------------------------------------------------------
# 4. abstention empty -> write nothing (contract 4)
# ---------------------------------------------------------------------------


async def test_abstained_writes_nothing(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # abstained AuditedGrade -> convert_findings_to_events returns ()
    audited = _audited([_covered_finding()], abstained=True)
    result = await persist_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(audited),
        done_ts=done_ts,
        parser_confidence=0.9,
        canon_key_by_canonical_key=by_key,
    )
    await db_session.commit()

    assert result == LearnerUpdateResult(
        events_written=0, states_upserted=0, skipped_unmapped=(), abstained=True
    )
    me = (await db_session.execute(
        select(func.count()).select_from(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    assert me == 0


# ---------------------------------------------------------------------------
# 5. run_learner_update single commit + entity-map re-derive (contract 2)
# ---------------------------------------------------------------------------


async def test_run_learner_update_commits(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    audited = _audited([_covered_finding()])
    result = await run_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(audited),
        done_ts=done_ts,
        parser_confidence=0.9,
    )
    assert result is not None
    assert result.events_written == 1
    assert result.states_upserted == 1

    me = (await db_session.execute(
        select(func.count()).select_from(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    assert me == 1


# ---------------------------------------------------------------------------
# 6. all-or-nothing rollback (contract 2)
# ---------------------------------------------------------------------------


async def test_layer3_atomic_rollback(db_session):
    """Force the learner-state upsert to raise AFTER the mastery-event append:
    the whole txn rolls back -> BOTH tables empty (the seed survives)."""
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    # Commit the seed so the rollback proves the L3 work (events + state) is
    # erased while the durable seed (course/entity/attempt) survives.
    await db_session.commit()
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    audited = _audited([_covered_finding()])

    boom = RuntimeError("forced mid-write failure")
    # Make the learner-state upsert blow up AFTER the mastery-event has been
    # added (db.add(MasteryEvent) runs, THEN _upsert_learner_state raises) — so
    # the test proves the append + upsert share ONE txn that rolls back BOTH on a
    # mid-write failure.
    with patch(
        "apollo.learner_model.persistence._upsert_learner_state",
        side_effect=boom,
    ):
        with pytest.raises(RuntimeError):
            await run_learner_update(
                db_session,
                sess=sess,
                attempt=attempt,
                shadow=_shadow(audited),
                done_ts=done_ts,
                parser_confidence=0.9,
            )

    me = (await db_session.execute(
        select(func.count()).select_from(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    ls = (await db_session.execute(
        select(func.count()).select_from(LearnerState).where(LearnerState.user_id == TEST_USER_ID)
    )).scalar_one()
    assert me == 0
    assert ls == 0
    # the seed survived the L3 rollback (the attempt row is still there)
    surviving = (await db_session.execute(
        select(func.count()).select_from(ProblemAttempt).where(ProblemAttempt.id == attempt.id)
    )).scalar_one()
    assert surviving == 1


# ---------------------------------------------------------------------------
# 7. NO-FALLBACK never voids the grade (contract 3)
# ---------------------------------------------------------------------------


async def test_layer3_failure_sets_pending_keeps_grade(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    # The OLD grade/XP are durable BEFORE run_learner_update runs (production
    # invariant) — commit the seed so the Layer-3 rollback inside the NO-FALLBACK
    # fork does not erase the attempt row.
    await db_session.commit()
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    audited = _audited([_covered_finding()])
    boom = RuntimeError("layer3 down")
    with patch(
        "apollo.handlers.learner_update.persist_learner_update",
        new=AsyncMock(side_effect=boom),
    ):
        with pytest.raises(RuntimeError):
            await run_learner_update(
                db_session,
                sess=sess,
                attempt=attempt,
                shadow=_shadow(audited),
                done_ts=done_ts,
                parser_confidence=0.9,
            )

    refreshed = (await db_session.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == attempt.id)
    )).scalar_one()
    assert refreshed.learner_update_pending is True
    assert refreshed.result == "graded"  # the grade survives


# ---------------------------------------------------------------------------
# 8. SUPERSEDE recompute from the EVENT LOG (contract 8) — the two retry tests
# ---------------------------------------------------------------------------


async def test_retry_identical_idempotent(db_session):
    """Run twice with the same shadow -> the final belief equals the single-run
    belief (attempt-wide DELETE + recompute from the prior-ATTEMPT event-log
    base, never the self-mutated state row)."""
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    audited = _audited([_covered_finding()])

    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(audited),
        done_ts=done_ts, parser_confidence=0.9,
    )
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(audited),
        done_ts=done_ts, parser_confidence=0.9,
    )

    events = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalars().all()
    assert len(events) == 1  # supersede, not double

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # equals the single-run posterior (cold-start base both times)
    event = convert_findings_to_events(audited, opposes_map={}, turn_order={})[0]
    expected = _expected_update(
        event, parser_confidence=0.9, normalization_confidence=0.8, done_ts=done_ts
    )
    assert _belief_close(state.belief, expected.posterior_belief)
    # evidence_count incremented to 2 over the two runs (same (user,entity) row)
    assert state.evidence_count == 2


async def test_retry_changed_kind_recomputes(db_session):
    """First run emits a MISCONCEPTION for the entity; retry emits a CORRECTED
    for the SAME entity -> the posterior reflects ONLY the corrected event over
    the (cold-start) base, with no misconception residue."""
    sid, cid, by_key = await _seed_course_with_entity(
        db_session, canonical_keys=(_CONTINUITY,)
    )
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # Run 1: a standalone contradiction on eq.continuity -> misconception event.
    misc_audited = _audited([_misconception_finding(key=_CONTINUITY)])
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(misc_audited),
        done_ts=done_ts, parser_confidence=0.9,
    )
    ev1 = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalars().all()
    assert len(ev1) == 1
    assert ev1[0].event_kind == "misconception"

    # Run 2 (retry of SAME attempt): a plain covered -> corrected? Use a covered
    # event (kind change misconception -> covered) on the same entity.
    covered_audited = _audited([_covered_finding(key=_CONTINUITY, score=1.0)])
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(covered_audited),
        done_ts=done_ts, parser_confidence=0.9,
    )

    events = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalars().all()
    # the misconception row is superseded; only the covered remains
    assert [e.event_kind for e in events] == ["covered"]

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # posterior reflects ONLY the covered event over the cold-start base (no
    # misconception residue, no double-count)
    event = convert_findings_to_events(covered_audited, opposes_map={}, turn_order={})[0]
    expected = _expected_update(
        event, parser_confidence=0.9, normalization_confidence=0.8, done_ts=done_ts
    )
    assert _belief_close(state.belief, expected.posterior_belief)


# ---------------------------------------------------------------------------
# 8b. same-entity multi-event folds into ONE belief update (§3 step 1 product)
# ---------------------------------------------------------------------------


async def test_same_entity_multi_event_folds_once(db_session):
    """Two events for the SAME entity within ONE Done (a contradiction + a covered
    on the same canonical_key, reachable today via the structurally-empty
    opposes_map -> _emit_standalone fires BOTH) must fold into ONE belief update:
    the per-entity likelihood PRODUCT (§3 step 1 'multiply per evidence item').

    Asserts: (a) each event still gets its OWN apollo_mastery_events row (the event
    log), (b) EXACTLY ONE apollo_learner_state row with evidence_count == 1 (the
    update 'fires once per Done episode' — NOT once per event), (c) the persisted
    posterior equals the SPEC §3 single combined-likelihood update — multiply the
    per-evidence RAW likelihoods into ONE L (step 1), damp the COMBINED L ONCE
    (step 2), then ONE bayes_update (step 3) — NOT a per-event apply_event chain.
    The affine damper is NOT multiplicative-homomorphic, so chaining (damp+bayes
    per event) diverges from the spec when q < 1; this asserts the combined result.
    """
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # Both a contradiction AND a covered on the SAME entity (empty opposes_map ->
    # no conflict resolution -> both emitted standalone for eq.continuity).
    audited = _audited(
        [
            _misconception_finding(key=_CONTINUITY),
            _covered_finding(key=_CONTINUITY, score=1.0),
        ]
    )
    shadow = _shadow(audited, normalization_confidence=0.8)

    result = await persist_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=shadow,
        done_ts=done_ts,
        parser_confidence=0.9,
        canon_key_by_canonical_key=by_key,
    )
    await db_session.commit()

    # Both events are logged, but only ONE entity is upserted.
    assert result.events_written == 2
    assert result.states_upserted == 1

    events = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalars().all()
    assert len(events) == 2  # both event-log rows present

    states = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalars().all()
    assert len(states) == 1  # ONE upsert, not two
    state = states[0]
    # fires ONCE per Done episode -> evidence_count is 1, not 2
    assert state.evidence_count == 1
    assert state.last_evidence_at == done_ts

    # the posterior is the SPEC §3 single combined-likelihood update over both
    # events (covered, then misconception): multiply the RAW per-evidence
    # likelihoods into ONE L (step 1), damp the COMBINED L ONCE (step 2), then ONE
    # bayes_update over the cold-start base (step 3). NOT a per-event apply_event
    # chain (which would damp + renormalize each event separately and diverge for
    # q = parser*grader < 1).
    ordered = convert_findings_to_events(audited, opposes_map={}, turn_order={})
    assert [e.event_kind.value for e in ordered] == ["covered", "misconception"]
    q = 0.9 * (0.8 * 1.0)  # parser_confidence * grader_confidence (comparison=1.0)
    combined_l = (1.0, 1.0, 1.0)
    for ev in ordered:
        L = likelihood_for_event(ev)
        combined_l = tuple(a * b for a, b in zip(combined_l, L, strict=True))
    expected_belief = bayes_update(COLD_START_PRIOR, damp(combined_l, q))
    assert _belief_close(state.belief, expected_belief)
    assert round(state.mastery, 9) == round(mastery_of(expected_belief), 9)


# ---------------------------------------------------------------------------
# 8c. misconception code surfaces on the COMBINED posterior (§3 two-step flag)
# ---------------------------------------------------------------------------


async def test_misconception_code_surfaces_on_second_attempt(db_session):
    """The §3 two-step misconception flag surfaces a code only once p_misc is the
    argmax AND >= 0.5 — 'typically only on the 2nd misconception'. A first attempt
    emits a misconception (code withheld, p_misc below threshold); a SECOND distinct
    attempt emits a misconception again over the prior-ATTEMPT event-log base, where
    the combined posterior clears the flag, so misconception_code is recorded on the
    learner_state AND on the new mastery_event row (drives _combined_belief_update's
    code-surfacing branch)."""
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt1 = await _seed_session_attempt(db_session, sid=sid, cid=cid, problem_code="p_m1")
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    misc_audited = _audited([_misconception_finding(key=_CONTINUITY)])

    await run_learner_update(
        db_session, sess=sess, attempt=attempt1, shadow=_shadow(misc_audited),
        done_ts=done_ts, parser_confidence=0.9,
    )
    state1 = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # 1st misconception from cold-start: p_misc not yet argmax -> code withheld.
    assert state1.misconception_code is None

    # 2nd distinct attempt: a misconception over the misc-leaning prior-attempt base.
    attempt2 = ProblemAttempt(
        session_id=sess.id, problem_id="p_m2", difficulty="intro", result="graded"
    )
    db_session.add(attempt2)
    await db_session.flush()
    await run_learner_update(
        db_session, sess=sess, attempt=attempt2, shadow=_shadow(misc_audited),
        done_ts=done_ts, parser_confidence=0.9,
    )

    state2 = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # combined posterior clears the §3 two-step flag -> the code is now recorded.
    assert state2.misconception_code == _CONTINUITY
    me2 = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt2.id)
    )).scalar_one()
    assert me2.misconception_code == _CONTINUITY


# ---------------------------------------------------------------------------
# 9. evidence_count increments across DISTINCT attempts (contract 10)
# ---------------------------------------------------------------------------


async def test_evidence_count_increments(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt1 = await _seed_session_attempt(db_session, sid=sid, cid=cid, problem_code="p_a")
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    audited = _audited([_covered_finding()])

    await run_learner_update(
        db_session, sess=sess, attempt=attempt1, shadow=_shadow(audited),
        done_ts=done_ts, parser_confidence=0.9,
    )
    state1 = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    assert state1.evidence_count == 1  # fresh insert

    # a SECOND distinct attempt for the same (user, entity)
    attempt2 = ProblemAttempt(
        session_id=sess.id, problem_id="p_b", difficulty="intro", result="graded"
    )
    db_session.add(attempt2)
    await db_session.flush()
    await run_learner_update(
        db_session, sess=sess, attempt=attempt2, shadow=_shadow(audited),
        done_ts=done_ts, parser_confidence=0.9,
    )
    state2 = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    assert state2.evidence_count == 2  # incremented on upsert


# ---------------------------------------------------------------------------
# 11. real CHECKs + UNIQUE NULLS NOT DISTINCT (must run on PG)
# ---------------------------------------------------------------------------


# The exact CHECK definitions from migration 026 (the schema authority). The ORM
# declares NO CHECKs (repo convention — migration SQL owns them), and the test
# harness builds tables via Base.metadata.create_all, so these constraints are
# applied here to the live connection to exercise the GENUINE PG CHECK behavior
# (mirrors tests/database/test_apollo_learner_model_migration.py, which applies
# the actual migration DDL). They live on apollo_learner_state per migration 026.
_LEARNER_STATE_CHECKS = (
    "ALTER TABLE apollo_learner_state ADD CONSTRAINT _t_belief_len3 "
    "CHECK (array_length(belief, 1) = 3)",
    "ALTER TABLE apollo_learner_state ADD CONSTRAINT _t_mastery_range "
    "CHECK (mastery >= 0 AND mastery <= 1)",
    "ALTER TABLE apollo_learner_state ADD CONSTRAINT _t_confidence_range "
    "CHECK (confidence >= 0 AND confidence <= 1)",
)


async def _apply_learner_state_checks(db) -> None:
    for ddl in _LEARNER_STATE_CHECKS:
        await db.execute(text(ddl))


async def test_belief_length_3_check(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    await _apply_learner_state_checks(db_session)
    # a length-2 belief violates the REAL CHECK (array_length(belief,1)=3)
    db_session.add(LearnerState(
        user_id=TEST_USER_ID, search_space_id=sid, entity_id=by_key[_CONTINUITY],
        belief=[0.5, 0.5], mastery=0.5, confidence=0.5, misconception_code=None,
        evidence_count=1, last_evidence_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_mastery_range_check(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    await _apply_learner_state_checks(db_session)
    # mastery 1.5 is out of [0,1] -> the REAL CHECK rejects it
    db_session.add(LearnerState(
        user_id=TEST_USER_ID, search_space_id=sid, entity_id=by_key[_CONTINUITY],
        belief=[0.3, 0.3, 0.4], mastery=1.5, confidence=0.5, misconception_code=None,
        evidence_count=1, last_evidence_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_unique_attempt_entity_kind(db_session):
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)

    def _mk():
        return MasteryEvent(
            user_id=TEST_USER_ID, search_space_id=sid, entity_id=by_key[_CONTINUITY],
            attempt_id=attempt.id, event_kind="covered", score=1.0,
            parser_confidence=0.9, grader_confidence=0.8,
            prior_belief=[0.2, 0.6, 0.2], posterior_belief=[0.1, 0.5, 0.4],
            mastery_after=0.65, evidence_node_ids=[],
        )

    db_session.add(_mk())
    await db_session.flush()
    db_session.add(_mk())  # duplicate (attempt_id, entity_id, event_kind)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# 10 & 12. done_ts single instant + flag-off byte-identity via the full route
# ---------------------------------------------------------------------------


def _route_student_graph(attempt_id: int) -> KGGraph:
    node = build_node(
        node_type="equation",
        node_id="stu_continuity",
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity", "variables": []},
        parser_confidence=0.95,
    )
    return KGGraph(nodes=[node], edges=[])


def _adjudicator_stub(_request):
    return {"stu_continuity": _CONTINUITY}


def _auditor_stub(_request):
    return {"spans": {}}


def _route_neo_stubs(attempt_id: int, *, stamp_mock):
    return [
        patch("apollo.handlers.done.KGStore.read_graph",
              new=AsyncMock(return_value=_route_student_graph(attempt_id))),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=stamp_mock),
        patch("apollo.handlers.done_grading.write_resolution", new=AsyncMock(return_value=None)),
        patch("apollo.handlers.done_turn_order.KGStore.read_node_created_at",
              new=AsyncMock(return_value={"stu_continuity": "2026-06-18T00:00:02+00:00"})),
        patch("apollo.handlers.done_grading.main_chat_adjudicator", new=_adjudicator_stub),
        patch("apollo.handlers.done_grading.main_chat_auditor", new=_auditor_stub),
    ]


async def _seed_route_session(db, *, current_code, sid, cid):
    sess = ApolloSession(
        user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid,
        status=SessionStatus.active.value, phase=SessionPhase.SOLVING.value,
        current_problem_id=current_code,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(session_id=sess.id, problem_id=current_code, difficulty="intro")
    db.add(attempt)
    await db.flush()
    db.add(Message(session_id=sess.id, attempt_id=attempt.id, role="student",
                   content="continuity says rho A1 v1 equals rho A2 v2", turn_index=0))
    await db.flush()
    return sess, attempt


async def _seed_route_entity(db, *, concept_id):
    """Seed eq.continuity entity under the bernoulli concept so the route's
    load_entity_specs map resolves the covered event."""
    ent = KGEntity(
        concept_id=concept_id, canonical_key=_CONTINUITY, kind="equation",
        display_name="Continuity", payload={}, aliases=[],
    )
    db.add(ent)
    await db.flush()
    return ent.id


async def test_done_ts_single_instant(db_session, monkeypatch):
    """Flag ON: the persisted LearnerState.last_evidence_at equals the `ts`
    threaded into the (stubbed) stamp_graded_at — ONE done_ts reaches both."""
    sid, cid, codes = await seed_course(
        db_session, subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle", problems=_INTRO,
    )
    entity_id = await _seed_route_entity(db_session, concept_id=cid)
    sess, attempt = await _seed_route_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    stamp_mock = AsyncMock(return_value=1)
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    patches = _route_neo_stubs(attempt.id, stamp_mock=stamp_mock)
    for p in patches:
        p.start()
    try:
        await handle_done(db=db_session, neo=object(), session_id=sess.id)
    finally:
        for p in reversed(patches):
            p.stop()

    # the ts passed to stamp_graded_at
    stamp_mock.assert_awaited_once()
    ts_arg = stamp_mock.await_args.kwargs["ts"]
    assert isinstance(ts_arg, datetime)

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == entity_id)
    )).scalar_one()
    assert state.last_evidence_at == ts_arg


async def test_layer3_flag_off_no_write(db_session, monkeypatch):
    """Contract 1: shadow ON but Layer-3 flag OFF -> the gated call never fires,
    NO mastery events / learner state written, grade committed."""
    sid, cid, codes = await seed_course(
        db_session, subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle", problems=_INTRO,
    )
    await _seed_route_entity(db_session, concept_id=cid)
    sess, attempt = await _seed_route_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    stamp_mock = AsyncMock(return_value=1)
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    patches = _route_neo_stubs(attempt.id, stamp_mock=stamp_mock)
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db_session, neo=object(), session_id=sess.id)
    finally:
        for p in reversed(patches):
            p.stop()

    assert "rubric" in out and "xp_earned" in out
    me = (await db_session.execute(
        select(func.count()).select_from(MasteryEvent).where(MasteryEvent.user_id == TEST_USER_ID)
    )).scalar_one()
    ls = (await db_session.execute(
        select(func.count()).select_from(LearnerState).where(LearnerState.user_id == TEST_USER_ID)
    )).scalar_one()
    assert me == 0
    assert ls == 0
    refreshed = (await db_session.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == attempt.id)
    )).scalar_one()
    assert refreshed.result == "graded"
    assert refreshed.learner_update_pending is False


# ---------------------------------------------------------------------------
# 13. WU-5B1 — APOLLO_LEARNER_DECAY_ENABLED ON decays the recompute-base prior
# ---------------------------------------------------------------------------


async def test_decay_flag_on_decays_base_prior(db_session, monkeypatch):
    """Flag ON: a SECOND attempt whose recompute-base prior is the FIRST attempt's
    event-log posterior, separated by a dt gap, persists a posterior that reflects
    the DECAYED base (and is numerically DISTINCT from the no-decay posterior).

    Independent oracle: the expected posterior is recomputed INLINE from the frozen
    WU-5A1 primitives + ``decay_toward_prior`` (the §3 Step-0 -> Step-3 chain), NOT
    by calling the production fold. Also asserts the closure's ``dt_days is None``
    cold-start branch is identity (attempt1 from cold-start, no prior anchor)."""
    monkeypatch.setenv("APOLLO_LEARNER_DECAY_ENABLED", "1")

    sid, cid, by_key = await _seed_course_with_entity(db_session)
    entity_id = by_key[_CONTINUITY]

    # --- attempt1: cold-start (prior_last_evidence_at is None -> closure dt=None) ---
    sess, attempt1 = await _seed_session_attempt(
        db_session, sid=sid, cid=cid, problem_code="p_decay1"
    )
    done_ts1 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    covered_audited = _audited([_covered_finding(score=1.0)])

    await run_learner_update(
        db_session, sess=sess, attempt=attempt1, shadow=_shadow(covered_audited),
        done_ts=done_ts1, parser_confidence=0.9,
    )

    # The closure's dt_days-is-None guard: from cold-start the flag-ON posterior is
    # IDENTICAL to the no-decay cold-start posterior (no anchor -> transform identity).
    state1 = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == entity_id)
    )).scalar_one()
    covered_event = convert_findings_to_events(
        covered_audited, opposes_map={}, turn_order={}
    )[0]
    q1 = 0.9 * (0.8 * 1.0)  # parser * (normalization * comparison)
    base = bayes_update(
        COLD_START_PRIOR, damp(likelihood_for_event(covered_event), q1)
    )
    assert _belief_close(state1.belief, base)  # cold-start: dt=None -> identity
    assert state1.last_evidence_at == done_ts1

    # --- attempt2: a 7-day gap -> integer dt_days_since_last = 7 -> w ~ 0.295 ---
    attempt2 = ProblemAttempt(
        session_id=sess.id, problem_id="p_decay2", difficulty="intro", result="graded"
    )
    db_session.add(attempt2)
    await db_session.flush()
    done_ts2 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)  # (done_ts2 - done_ts1).days == 7

    await run_learner_update(
        db_session, sess=sess, attempt=attempt2, shadow=_shadow(covered_audited),
        done_ts=done_ts2, parser_confidence=0.9,
    )

    state2 = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == entity_id)
    )).scalar_one()

    # Independent oracle (§3 Step 0 -> Step 3): the recompute base is attempt1's
    # event-log posterior (== `base`); decay it toward COLD_START_PRIOR by dt=7,
    # THEN the same single-event damp/bayes.
    q2 = 0.9 * (0.8 * 1.0)
    combined_l = likelihood_for_event(covered_event)
    decayed_base = decay_toward_prior(base, COLD_START_PRIOR, 7)
    expected = bayes_update(decayed_base, damp(combined_l, q2))
    assert _belief_close(state2.belief, expected)

    # Distinctness: the NO-decay posterior (over the raw base) is numerically
    # DISTINCT from the persisted decayed posterior (proves decay actually fired).
    no_decay_expected = bayes_update(base, damp(combined_l, q2))
    assert not _belief_close(state2.belief, no_decay_expected)
    # And the recorded event row carries dt_days_since_last == 7 (the hoisted dt).
    me2 = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt2.id)
    )).scalar_one()
    assert me2.dt_days_since_last == 7


# ---------------------------------------------------------------------------
# 14. WU-5B2 — APOLLO_LEARNER_NEGOTIATION_ENABLED §3 negotiation multiplier
# ---------------------------------------------------------------------------
#
# The fold READS apollo_kg_negotiations (actor='student', latest-wins) and, when
# the flag is ON, multiplies a qualifying entity's COMBINED likelihood by
# NEGOTIATION_LIKELIHOOD ONCE BEFORE damp, persists the representative
# negotiation_move on each event row, and SUPPRESSES the multiplier (-> identity)
# on a misconception entity. Independent oracles are recomputed INLINE from the
# frozen belief.py + negotiation.py constants (never the production fold).


async def _seed_negotiation(
    db, *, attempt_id, entry_id, move, actor="student", created_at=None
):
    """Seed one apollo_kg_negotiations row (mirrors the LearnerState seed pattern).
    KGNegotiation is in Base.metadata so the integration tests seed it directly."""
    db.add(
        KGNegotiation(
            attempt_id=attempt_id,
            entry_id=entry_id,
            move=move,
            actor=actor,
            payload={},
            **({"created_at": created_at} if created_at else {}),
        )
    )
    await db.flush()


def _covered_oracle(*, score, q, multiplier=None):
    """The INLINE oracle for a single covered@score event over the cold-start base:
    combined L = covered_likelihood(score) (x) multiplier (identity when None),
    multiplier BEFORE damp, then damp(q) and one bayes_update."""
    combined = likelihood_for_event(
        convert_findings_to_events(
            _audited([_covered_finding(score=score)]), opposes_map={}, turn_order={}
        )[0]
    )
    if multiplier is not None:
        combined = tuple(a * b for a, b in zip(combined, multiplier, strict=True))
    return bayes_update(COLD_START_PRIOR, damp(combined, q))


_Q = 0.9 * (0.8 * 1.0)  # parser_confidence * (normalization_confidence * comparison)


async def test_negotiation_flag_on_covered_entity_gets_boost(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_1", move="challenge"
    )
    audited = _audited([_covered_finding(score=1.0)])
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(audited),
        done_ts=done_ts, parser_confidence=0.9,
    )

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    boosted = _covered_oracle(score=1.0, q=_Q, multiplier=NEGOTIATION_LIKELIHOOD)
    no_boost = _covered_oracle(score=1.0, q=_Q)
    assert _belief_close(state.belief, boosted)
    # distinctness: the boost actually fired (boosted != no-boost posterior).
    assert not _belief_close(state.belief, no_boost)


async def test_negotiation_flag_on_misconception_entity_unchanged(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    # the misconception entity's key must be mapped so the event is persisted.
    sid, cid, by_key = await _seed_course_with_entity(db_session, canonical_keys=(_CONTINUITY,))
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # a misconception finding on _CONTINUITY (node stu_m) WITH a qualifying move.
    misc_audited = _audited([_misconception_finding(key=_CONTINUITY)])
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_m", move="paraphrase"
    )
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(misc_audited),
        done_ts=done_ts, parser_confidence=0.9,
    )

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # suppression -> identity: the persisted posterior is the no-negotiation
    # misconception posterior (combined L = misconception only).
    misc_event = convert_findings_to_events(misc_audited, opposes_map={}, turn_order={})[0]
    misc_only = bayes_update(COLD_START_PRIOR, damp(likelihood_for_event(misc_event), _Q))
    assert _belief_close(state.belief, misc_only)
    # and DISTINCT from the (rejected) boosted misconception posterior.
    misc_l = likelihood_for_event(misc_event)
    boosted = bayes_update(
        COLD_START_PRIOR,
        damp(
            tuple(a * b for a, b in zip(misc_l, NEGOTIATION_LIKELIHOOD, strict=True)),
            _Q,
        ),
    )
    assert not _belief_close(state.belief, boosted)


async def test_negotiation_move_persisted_on_event_row(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_1", move="challenge"
    )
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(_audited([_covered_finding()])),
        done_ts=done_ts, parser_confidence=0.9,
    )

    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    # the override replaced the frozen MasteryEventRowSpec.negotiation_move=None.
    assert me.negotiation_move == "challenge"


async def test_negotiation_latest_wins_two_moves_one_node(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    t0 = datetime(2026, 6, 18, 11, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 18, 11, 5, 0, tzinfo=UTC)  # later
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_1", move="paraphrase", created_at=t0
    )
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_1", move="challenge", created_at=t1
    )
    await run_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(_audited([_covered_finding(score=1.0)])),
        done_ts=done_ts,
        parser_confidence=0.9,
    )

    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    assert me.negotiation_move == "challenge"  # latest-wins
    # and the belief still reflects the (qualifying) boost.
    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    assert _belief_close(
        state.belief, _covered_oracle(score=1.0, q=_Q, multiplier=NEGOTIATION_LIKELIHOOD)
    )


async def test_negotiation_actor_filter_ignores_non_student(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # ONLY a parser-actor row (later created_at) -> the student read IGNORES it.
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_1", move="challenge", actor="parser",
    )
    await run_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(_audited([_covered_finding(score=1.0)])),
        done_ts=done_ts,
        parser_confidence=0.9,
    )

    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    assert me.negotiation_move is None  # parser row ignored -> no move
    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # no student move -> no boost -> the plain covered posterior.
    assert _belief_close(state.belief, _covered_oracle(score=1.0, q=_Q))


async def test_negotiation_skip_is_noop(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    await _seed_negotiation(db_session, attempt_id=attempt.id, entry_id="stu_1", move="skip")
    await run_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(_audited([_covered_finding(score=1.0)])),
        done_ts=done_ts,
        parser_confidence=0.9,
    )

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # skip -> identity multiplier -> byte-identical to the no-negotiation posterior.
    assert _belief_close(state.belief, _covered_oracle(score=1.0, q=_Q))
    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    # but the skip move IS recorded (the corpus keeps it even though it doesn't boost).
    assert me.negotiation_move == "skip"


async def test_negotiation_no_move_for_entity_identity(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # a move exists for a DIFFERENT node not in this entity's evidence_node_ids.
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="other_node", move="challenge"
    )
    await run_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(_audited([_covered_finding(score=1.0)])),
        done_ts=done_ts,
        parser_confidence=0.9,
    )

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    assert _belief_close(state.belief, _covered_oracle(score=1.0, q=_Q))
    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    assert me.negotiation_move is None


async def test_negotiation_flag_off_byte_identical(db_session, monkeypatch):
    monkeypatch.delenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", raising=False)
    sid, cid, by_key = await _seed_course_with_entity(db_session)
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    # a qualifying challenge row is seeded BUT the flag is OFF -> identity path.
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_1", move="challenge"
    )
    await run_learner_update(
        db_session,
        sess=sess,
        attempt=attempt,
        shadow=_shadow(_audited([_covered_finding(score=1.0)])),
        done_ts=done_ts,
        parser_confidence=0.9,
    )

    state = (await db_session.execute(
        select(LearnerState).where(LearnerState.entity_id == by_key[_CONTINUITY])
    )).scalar_one()
    # byte-identical to the WU-5A2/5B1 no-negotiation posterior.
    assert _belief_close(state.belief, _covered_oracle(score=1.0, q=_Q))
    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    assert me.negotiation_move is None  # flag OFF -> no override


async def test_negotiation_suppressed_then_move_persisted(db_session, monkeypatch):
    """I9: suppression mutes the BELIEF multiplier but the move STRING is STILL
    persisted (the two closures are decoupled) — so the refit corpus keeps it."""
    monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")
    sid, cid, by_key = await _seed_course_with_entity(db_session, canonical_keys=(_CONTINUITY,))
    sess, attempt = await _seed_session_attempt(db_session, sid=sid, cid=cid)
    done_ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)

    misc_audited = _audited([_misconception_finding(key=_CONTINUITY)])
    await _seed_negotiation(
        db_session, attempt_id=attempt.id, entry_id="stu_m", move="paraphrase"
    )
    await run_learner_update(
        db_session, sess=sess, attempt=attempt, shadow=_shadow(misc_audited),
        done_ts=done_ts, parser_confidence=0.9,
    )

    me = (await db_session.execute(
        select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )).scalar_one()
    # belief suppressed (asserted in I2) but the move string IS recorded.
    assert me.negotiation_move == "paraphrase"
