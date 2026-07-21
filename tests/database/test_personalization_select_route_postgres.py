"""WU-6A3 — real-PG acceptance gate: the LIVE session-personalization wiring.

Drives the v1 selection wedge end-to-end on the real ``db_session`` (Testcontainers
pgvector:pg16, ``Base.metadata.create_all``, savepoint rollback per test):

* the flag module ``apollo.overseer.personalization_flag.is_enabled``;
* the new ``apollo.overseer.problem_selector.select_problem_personalized`` (reads
  the frozen WU-6A1 ``LearnerProfile`` once, delegates scoring to the frozen WU-6A2
  ``personalize_selection``, emits ONE structured observability log);
* BOTH swapped call-sites — ``apollo.handlers.next.handle_next`` (``/next``) and
  ``apollo.hoot_bridge.session_init.init_session_from_hoot`` (the LITERAL session
  start).

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate. The
per-classroom isolation invariant (spec §1.4 — mastery never crosses courses) is the
join semantics under test, so it is pinned on real Postgres, not SQLite.

Deps mocked: ONLY the env flag (``monkeypatch.setenv``/``delenv`` on
``APOLLO_SESSION_PERSONALIZATION_ENABLED``) and, on the ``init_session_from_hoot``
path, the upstream concept-inference LLM hop (``patch(...infer_concept_id...)`` —
the established harness). The selection seam itself
(``select_problem_personalized`` -> ``read_learner_profile`` ->
``personalize_selection``) is pure-PG + pure-Python: no DB mock, no LLM/network.
The log is captured with ``caplog`` on logger ``apollo.overseer.problem_selector``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from apollo.conftest import TEST_USER_ID
from apollo.errors import PoolExhaustedError
from apollo.handlers.next import handle_next
from apollo.hoot_bridge.session_init import init_session_from_hoot
from apollo.overseer import personalization_flag
from apollo.overseer.problem_selector import (
    select_problem,
    select_problem_personalized,
)
from apollo.persistence.models import (
    EntityPrereq,
    KGEntity,
    LearnerState,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    problem_database_id,
    seed_course,
)

pytestmark = pytest.mark.integration

_FLAG = "APOLLO_SESSION_PERSONALIZATION_ENABLED"
_LOGGER = "apollo.overseer.problem_selector"

# The canonical-key sets (reconstructed from the bernoulli reference solutions via
# the frozen _ENTRY_TYPE_TO_KIND_PREFIX map — the same rule WU-6A2 uses). Pinned
# here as the test oracle for the discriminating coverage assertions.
_P1 = "bernoulli_horizontal_pipe_find_p2"  # intro, covers cont+incomp+bernoulli
_P2 = "bernoulli_height_change_find_v2"  # intro, covers bernoulli
_P3 = "continuity_area_change_find_v2"  # intro, covers cont+incomp
_P4 = "volumetric_flow_rate_find_Q"  # intro, covers flow_rate_definition (not weak)

_CONTINUITY = "eq.continuity"
_INCOMPRESSIBILITY = "cond.incompressibility"
_BERNOULLI = "eq.bernoulli"


# ---------------------------------------------------------------------------
# Direct-ORM seed helpers (real Postgres via db_session). Adapted from
# test_personalization_read_postgres.py (WU-6A1) + test_done_layer3_route_postgres.py
# (WU-5A2) + _curriculum_fixtures.py (WU-3D).
# ---------------------------------------------------------------------------


async def _seed_pool(db, *, subject_slug, concept_slug="bernoulli_principle"):
    """Seed a full course chain (search_space -> subject -> concept -> all 5
    bernoulli problems under ONE integer concept_id by directory-filing).

    Returns ``(search_space_id, concept_id, problem_codes)``. The pool holds all 5
    problems; ``difficulty="intro"`` selects among P1-P4 (P5 is standard). Tests
    assert on ``Problem.id``, never on ``Problem.concept_id`` (the JSON slug).
    """
    return await seed_course(
        db,
        subject_slug=subject_slug,
        concept_slug=concept_slug,
        problems=load_bernoulli_problem_payloads(),
    )


async def _seed_entity(db, *, concept_id, canonical_key, kind="equation") -> int:
    """Add one KGEntity under concept_id; return its id (WU-6A1 read-test pattern)."""
    ent = KGEntity(
        concept_id=concept_id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=canonical_key,
        payload={},
        aliases=[],
    )
    db.add(ent)
    await db.flush()
    return ent.id


async def _seed_prereq(db, *, from_id, to_id) -> None:
    """Add one EntityPrereq edge (from depends on to) (WU-6A1 pattern)."""
    db.add(EntityPrereq(from_entity_id=from_id, to_entity_id=to_id))
    await db.flush()


async def _seed_state(
    db,
    *,
    sid,
    entity_id,
    mastery,
    confidence,
    misconception_code=None,
    user_id=TEST_USER_ID,
) -> None:
    """Add one apollo_learner_state row (WU-6A1 pattern). ``belief`` is NOT NULL;
    a faithful length-3 vector is supplied."""
    belief = [round(1.0 - mastery, 4), 0.0, round(mastery, 4)]
    db.add(
        LearnerState(
            user_id=user_id,
            search_space_id=sid,
            entity_id=entity_id,
            belief=belief,
            mastery=mastery,
            confidence=confidence,
            misconception_code=misconception_code,
            evidence_count=1,
            last_evidence_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db.flush()


async def _seed_weak_profile(db, *, sid, cid):
    """Seed the three Example-A entities (eq.continuity, cond.incompressibility,
    eq.bernoulli) under ``cid`` and weak learner_state under ``(TEST_USER_ID, sid)``.

    Mastery values are strictly inside the [0.3, 0.7] teachable band; deficits
    (1 - mastery): continuity 0.65, incompressibility 0.60, bernoulli 0.32. No
    prereq edges => prereqs trivially mastered. Coverage ordering: P1 (all three)
    is max; with P1 attempted, P3 (cont+incomp = 1.25) > P2 (bernoulli = 0.32).
    """
    e_cont = await _seed_entity(db, concept_id=cid, canonical_key=_CONTINUITY)
    e_incomp = await _seed_entity(
        db, concept_id=cid, canonical_key=_INCOMPRESSIBILITY, kind="condition"
    )
    e_bern = await _seed_entity(db, concept_id=cid, canonical_key=_BERNOULLI)
    await _seed_state(db, sid=sid, entity_id=e_cont, mastery=0.35, confidence=0.6)
    await _seed_state(db, sid=sid, entity_id=e_incomp, mastery=0.40, confidence=0.6)
    await _seed_state(db, sid=sid, entity_id=e_bern, mastery=0.68, confidence=0.6)
    return {_CONTINUITY: e_cont, _INCOMPRESSIBILITY: e_incomp, _BERNOULLI: e_bern}


async def _seed_session(
    db, *, sid, cid, current_problem_id, phase=SessionPhase.REPORT.value, user_id=TEST_USER_ID
):
    """Seed one TutoringSession (WU-5A2 pattern) for the handle_next route tests."""
    database_id = await problem_database_id(
        db, concept_id=cid, problem_code=current_problem_id
    )
    sess = TutoringSession(
        user_id=user_id,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=phase,
        current_problem_id=database_id,
    )
    db.add(sess)
    await db.flush()
    return sess


async def _seed_attempt(db, *, session_id, problem_id, difficulty="intro", result="graded"):
    session = await db.get(TutoringSession, session_id)
    database_id = await problem_database_id(
        db, concept_id=session.concept_id, problem_code=problem_id
    )
    attempt = ProblemAttempt(
        session_id=session_id,
        problem_id=database_id,
        difficulty=difficulty,
        result=result,
        user_id=session.user_id,
        course_id=session.course_id,
    )
    db.add(attempt)
    await db.flush()
    return attempt


def _personalized_records(caplog):
    return [
        rec for rec in caplog.records if getattr(rec, "event", None) == "personalized_selection"
    ]


# ===========================================================================
# A. Flag module (unit — no DB)
# ===========================================================================


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "On"])
def test_flag_is_enabled_truthy_set(monkeypatch, value):
    """Every case-insensitive truthy value enables the flag (mirrors
    misconception.is_enabled)."""
    monkeypatch.setenv(_FLAG, value)
    assert personalization_flag.is_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_flag_is_disabled_for_falsey_values(monkeypatch, value):
    """Falsey / unrecognized values keep the flag OFF."""
    monkeypatch.setenv(_FLAG, value)
    assert personalization_flag.is_enabled() is False


def test_flag_is_disabled_when_unset(monkeypatch):
    """Unset => OFF (the default-OFF prod posture)."""
    monkeypatch.delenv(_FLAG, raising=False)
    assert personalization_flag.is_enabled() is False


# ===========================================================================
# B. select_problem_personalized unit-level (driven directly, real PG)
# ===========================================================================


async def test_flag_off_byte_identical_to_select_problem(db_session, monkeypatch):
    """FLAG-OFF anchor: select_problem_personalized returns the EXACT same Problem.id
    as the untouched select_problem for a seeded pool. The load-bearing live
    non-regression guard at the unit seam."""
    monkeypatch.delenv(_FLAG, raising=False)
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_off")

    baseline = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[],
    )
    chosen = await select_problem_personalized(
        db_session,
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        difficulty="intro",
        attempted_ids=[],
    )
    assert chosen.id == baseline.id


async def test_flag_off_emits_no_personalized_log(db_session, monkeypatch, caplog):
    """FLAG-OFF is silent: no personalized_selection log record fires."""
    monkeypatch.delenv(_FLAG, raising=False)
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_off_log")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            concept_id=cid,
            difficulty="intro",
            attempted_ids=[],
        )
    assert _personalized_records(caplog) == []


async def test_flag_on_empty_state_returns_candidates_zero(db_session, monkeypatch):
    """FLAG-ON + empty apollo_learner_state (the PROD cold-start path): returns
    byte-identically to select_problem (candidates[0])."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_empty")
    # entities may be seeded; with ZERO state rows is_empty=True either way.
    await _seed_entity(db_session, concept_id=cid, canonical_key=_CONTINUITY)

    baseline = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[],
    )
    chosen = await select_problem_personalized(
        db_session,
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        difficulty="intro",
        attempted_ids=[],
    )
    assert chosen.id == baseline.id


async def test_flag_on_empty_state_log_marks_fallback(db_session, monkeypatch, caplog):
    """FLAG-ON + empty state: exactly ONE personalized_selection record marks the
    cold-start fallback (profile_is_empty, n_weak_entities==0, fallback_fired)."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_empty_log")
    baseline = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[],
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            concept_id=cid,
            difficulty="intro",
            attempted_ids=[],
        )

    records = _personalized_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.personalization_enabled is True
    assert rec.profile_is_empty is True
    assert rec.n_weak_entities == 0
    assert rec.fallback_fired is True
    assert rec.chosen_problem_id == baseline.id


async def test_flag_on_seeded_weak_selects_high_coverage_problem(db_session, monkeypatch):
    """FLAG-ON + a weak Example-A profile: the wedge fires and picks the higher
    coverage problem at difficulty='intro'.

    No attempt: P1 (covers all three weak entities) is the max-coverage pick.
    With P1 attempted: the remaining intro candidates discriminate by COVERAGE, not
    list order — P3 (cont+incomp = 1.25) over P2 (bernoulli = 0.32) over P4 (0.0).
    """
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_weak")
    await _seed_weak_profile(db_session, sid=sid, cid=cid)

    chosen = await select_problem_personalized(
        db_session,
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        difficulty="intro",
        attempted_ids=[],
    )
    assert chosen.id == _P1

    # Discrimination is non-vacuous: with P1 attempted the coverage-max remaining
    # intro candidate is P3 (NOT the first-in-list P2).
    chosen_after = await select_problem_personalized(
        db_session,
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        difficulty="intro",
        attempted_ids=[_P1],
    )
    assert chosen_after.id == _P3


async def test_flag_on_seeded_weak_log_marks_personalized(db_session, monkeypatch, caplog):
    """FLAG-ON + weak profile (no attempt): ONE personalized_selection record marks
    the wedge engaged (profile_is_empty False, n_weak_entities==3, fallback_fired
    False, chosen_problem_id==P1)."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_weak_log")
    await _seed_weak_profile(db_session, sid=sid, cid=cid)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            concept_id=cid,
            difficulty="intro",
            attempted_ids=[],
        )

    records = _personalized_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.personalization_enabled is True
    assert rec.profile_is_empty is False
    assert rec.n_weak_entities == 3
    assert rec.fallback_fired is False
    assert rec.chosen_problem_id == _P1


async def test_flag_on_prereq_blocked_weak_excluded_falls_back(db_session, monkeypatch, caplog):
    """FLAG-ON: the wiring threads WU-6A1's prereq edges into WU-6A2's gate. With
    eq.continuity the only in-band entity but its prereq (cond.incompressibility,
    unseen => 0.50 < 0.70) BLOCKING, weak == {} => fallback to candidates[0]."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_blocked")
    e_cont = await _seed_entity(db_session, concept_id=cid, canonical_key=_CONTINUITY)
    e_incomp = await _seed_entity(
        db_session, concept_id=cid, canonical_key=_INCOMPRESSIBILITY, kind="condition"
    )
    # eq.continuity depends on cond.incompressibility; only continuity has state.
    await _seed_prereq(db_session, from_id=e_cont, to_id=e_incomp)
    await _seed_state(db_session, sid=sid, entity_id=e_cont, mastery=0.40, confidence=0.6)

    baseline = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[],
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        chosen = await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            concept_id=cid,
            difficulty="intro",
            attempted_ids=[],
        )

    assert chosen.id == baseline.id
    records = _personalized_records(caplog)
    assert len(records) == 1
    assert records[0].fallback_fired is True
    assert records[0].n_weak_entities == 0


async def test_flag_on_pool_exhausted_hard_raises_byte_identical(db_session, monkeypatch, caplog):
    """FLAG-ON + difficulty='hard' (0 hard candidates in the seed): raises
    PoolExhaustedError byte-identically (concept_cluster_id=str(cid),
    difficulty='hard') BEFORE any log fires."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_hard")
    await _seed_weak_profile(db_session, sid=sid, cid=cid)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with pytest.raises(PoolExhaustedError) as exc_info:
            await select_problem_personalized(
                db_session,
                user_id=TEST_USER_ID,
                search_space_id=sid,
                concept_id=cid,
                difficulty="hard",
                attempted_ids=[],
            )
    assert exc_info.value.difficulty == "hard"
    assert exc_info.value.concept_cluster_id == str(cid)
    assert _personalized_records(caplog) == []


async def test_flag_on_pool_exhausted_flag_off_parity(db_session, monkeypatch):
    """select_problem ALSO raises at difficulty='hard' with the identical
    .difficulty/.concept_cluster_id — flag-ON exhaustion == flag-OFF exhaustion."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="s_hard_parity")
    await _seed_weak_profile(db_session, sid=sid, cid=cid)

    with pytest.raises(PoolExhaustedError) as off_exc:
        await select_problem(
            db_session,
            concept_id=cid,
            search_space_id=sid,
            difficulty="hard",
            attempted_ids=[],
        )
    with pytest.raises(PoolExhaustedError) as on_exc:
        await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            concept_id=cid,
            difficulty="hard",
            attempted_ids=[],
        )
    assert off_exc.value.difficulty == on_exc.value.difficulty == "hard"
    assert off_exc.value.concept_cluster_id == on_exc.value.concept_cluster_id == str(cid)


# ===========================================================================
# C. Course-isolation end-to-end (§1.4 invariant through the live seam)
# ===========================================================================


async def test_course_isolation_personalized_selection(db_session, monkeypatch, caplog):
    """FLAG-ON: course A has a weak profile; course B (distinct sid/cid) has only
    mastered entities. The course-B call uses course B's (empty-weak) profile ->
    candidates[0] / fallback; course A's weak mastery never crosses into B. Proves
    mastery never crosses courses through the live seam."""
    monkeypatch.setenv(_FLAG, "1")
    sid_a, cid_a, _ca = await _seed_pool(db_session, subject_slug="iso_a")
    sid_b, cid_b, _cb = await _seed_pool(db_session, subject_slug="iso_b")

    # Course A: weak Example-A profile (would personalize to P1).
    await _seed_weak_profile(db_session, sid=sid_a, cid=cid_a)
    # Course B: same entities but all MASTERED (> 0.7) => no weak-teachable.
    e_cont_b = await _seed_entity(db_session, concept_id=cid_b, canonical_key=_CONTINUITY)
    e_incomp_b = await _seed_entity(
        db_session, concept_id=cid_b, canonical_key=_INCOMPRESSIBILITY, kind="condition"
    )
    await _seed_state(db_session, sid=sid_b, entity_id=e_cont_b, mastery=0.92, confidence=0.9)
    await _seed_state(db_session, sid=sid_b, entity_id=e_incomp_b, mastery=0.88, confidence=0.9)

    baseline_b = await select_problem(
        db_session,
        concept_id=cid_b,
        search_space_id=sid_b,
        difficulty="intro",
        attempted_ids=[],
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        chosen_b = await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid_b,
            concept_id=cid_b,
            difficulty="intro",
            attempted_ids=[],
        )
    # Course B has weak == {} (all mastered) => fallback to candidates[0]; course A's
    # weak mastery never influenced it.
    assert chosen_b.id == baseline_b.id
    b_records = _personalized_records(caplog)
    assert len(b_records) == 1
    assert b_records[0].fallback_fired is True
    assert b_records[0].n_weak_entities == 0

    # Conversely course A DOES personalize (its profile is weak).
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        chosen_a = await select_problem_personalized(
            db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid_a,
            concept_id=cid_a,
            difficulty="intro",
            attempted_ids=[],
        )
    assert chosen_a.id == _P1
    a_records = _personalized_records(caplog)
    assert len(a_records) == 1
    assert a_records[0].fallback_fired is False


# ===========================================================================
# D. Route integration — handle_next (real PG)
# ===========================================================================


async def test_handle_next_flag_off_byte_identical(db_session, monkeypatch):
    """FLAG-OFF route anchor: handle_next returns the same problem.id as
    select_problem for the same state, and the payload SHAPE is unchanged."""
    monkeypatch.delenv(_FLAG, raising=False)
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="next_off")
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id=_P1)
    await _seed_attempt(db_session, session_id=sess.id, problem_id=_P1)
    await db_session.commit()

    expected = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[_P1],
    )

    payload = await handle_next(db=db_session, session_id=sess.id, difficulty="intro")

    assert payload["problem"]["id"] == expected.id
    assert set(payload.keys()) == {"session_id", "attempt_id", "problem"}
    assert set(payload["problem"].keys()) == {
        "id",
        "concept_id",
        "difficulty",
        "problem_text",
        "given_values",
        "target_unknown",
    }


async def test_handle_next_flag_on_seeded_weak_personalizes(db_session, monkeypatch, caplog):
    """FLAG-ON route: handle_next threads sess.user_id/search_space_id/concept_id
    into the personalized seam. With P1 already attempted and a weak profile, the
    personalized pick is P3 (coverage) over P2 (list order). ONE log fires."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="next_weak")
    await _seed_weak_profile(db_session, sid=sid, cid=cid)
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id=_P1)
    await _seed_attempt(db_session, session_id=sess.id, problem_id=_P1)
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        payload = await handle_next(db=db_session, session_id=sess.id, difficulty="intro")

    assert payload["problem"]["id"] == _P3
    records = _personalized_records(caplog)
    assert len(records) == 1
    assert records[0].chosen_problem_id == _P3


async def test_handle_next_flag_on_empty_state_matches_flag_off(db_session, monkeypatch):
    """FLAG-ON + no learner_state: handle_next returns the SAME problem.id as the
    flag-OFF run for an identical session (prod cold-start parity at the route)."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="next_empty")
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id=_P1)
    await _seed_attempt(db_session, session_id=sess.id, problem_id=_P1)
    await db_session.commit()

    expected = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[_P1],
    )

    payload = await handle_next(db=db_session, session_id=sess.id, difficulty="intro")
    assert payload["problem"]["id"] == expected.id


# ===========================================================================
# E. Route integration — init_session_from_hoot (the LITERAL session start)
# ===========================================================================


async def test_init_session_flag_off_byte_identical(db_session, monkeypatch):
    """FLAG-OFF init anchor: init_session_from_hoot picks the same problem.id as
    select_problem (attempted_ids=[]); payload SHAPE unchanged. The concept-inference
    LLM hop is mocked deterministically (no live OpenAI call)."""
    monkeypatch.delenv(_FLAG, raising=False)
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="init_off")

    expected = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[],
    )

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        payload = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="bernoulli stuff",
            difficulty="intro",
        )

    assert payload["problem"]["id"] == expected.id
    assert set(payload.keys()) == {"session_id", "attempt_id", "problem"}
    assert set(payload["problem"].keys()) == {
        "id",
        "concept_id",
        "difficulty",
        "problem_text",
        "given_values",
        "target_unknown",
    }


async def test_init_session_flag_on_seeded_weak_personalizes(db_session, monkeypatch, caplog):
    """FLAG-ON init: the wedge fires at the LITERAL session start (session_init.py).
    With attempted_ids=[] and a weak profile, the max-coverage pick is P1. ONE log
    fires with chosen_problem_id==P1."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="init_weak")
    await _seed_weak_profile(db_session, sid=sid, cid=cid)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
            payload = await init_session_from_hoot(
                db=db_session,
                user_id=TEST_USER_ID,
                search_space_id=sid,
                hoot_transcript="bernoulli stuff",
                difficulty="intro",
            )

    assert payload["problem"]["id"] == _P1
    records = _personalized_records(caplog)
    assert len(records) == 1
    assert records[0].chosen_problem_id == _P1


async def test_init_session_flag_on_empty_state_matches_flag_off(db_session, monkeypatch):
    """FLAG-ON + no learner_state: init_session_from_hoot picks the same problem.id
    as the flag-OFF run (cold-start parity at the init seam)."""
    monkeypatch.setenv(_FLAG, "1")
    sid, cid, _codes = await _seed_pool(db_session, subject_slug="init_empty")

    expected = await select_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        difficulty="intro",
        attempted_ids=[],
    )

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        payload = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="bernoulli stuff",
            difficulty="intro",
        )

    assert payload["problem"]["id"] == expected.id
