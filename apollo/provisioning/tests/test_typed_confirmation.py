"""Manual typed-path teacher-confirmation + async re-homing endpoints (DB-backed).

Covers the Required-tests block for section 3/4: confirmation persistence and
polling representation, approve/resume, edit-replacement and discard,
authorization, indefinite pending state, the honesty + teacher stamp, and the
durable re-homing job (queryable ``rehoming_failed``, retry, and manual
existing-concept assignment). Each test drives the REAL production endpoints; the
only stubs are auth, the injected construction LLM, Neo4j, and the tag/mint +
canon-projection re-homing calls.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from apollo.persistence.models import (
    AuthoredSet,
    Concept,
    ConceptProblem,
    IngestRun,
    RehomingJob,
    RejectedProblem,
)
from apollo.provisioning.authored_sets.rehoming import ClaimedRehoming
from apollo.provisioning.promote import PromoteHeldForReview, PromoteResult


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


class _FakeRequest:
    pass


async def _fake_require_user(_request):
    from auth import AuthContext

    return AuthContext(user_id="teacher-1", access_token="token")


async def _fake_require_member(**_kwargs):
    return None


def _prose_steps() -> list[dict]:
    return [
        {
            "entry_type": "definition",
            "id": "federalism_meaning",
            "content": {"concept": "federalism", "meaning": "divided sovereignty"},
        },
        {
            "entry_type": "procedure_step",
            "id": "identify_veto_points",
            "content": {"action": "identify veto points", "purpose": "show checks on power"},
        },
        {
            "entry_type": "procedure_step",
            "id": "weigh_accountability",
            "content": {"action": "weigh veto points against blurred blame", "purpose": "answer"},
        },
    ]


class _ConstructMetered:
    """Only ``.main`` is used — the typed path never judges or tags at construct."""

    def main(self, *, purpose, **_kwargs):
        assert purpose == "authored_construct"
        return json.dumps({"steps": _prose_steps()})

    def cheap(self, *, purpose, **_kwargs):  # pragma: no cover - typed path never reaches these
        raise AssertionError(f"typed construction must not reach {purpose!r}")


async def _seed_course(db, *, slug: str) -> int:
    from apollo.persistence.models import Subject
    from database.models import SearchSpace

    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Civics")
    db.add(space)
    await db.flush()
    subject = Subject(slug=f"subject-{slug}", display_name="Civics", search_space_id=space.id)
    db.add(subject)
    await db.flush()
    return int(space.id)


async def _make_pending_draft(db_session, monkeypatch, *, slug: str):
    """Run the real create+background flow; return (aapi, space_id, set_id, problem_id)."""
    import apollo.provisioning.authored_sets.api as aapi

    space_id = await _seed_course(db_session, slug=slug)
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_kwargs: _ConstructMetered())
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: object())
    monkeypatch.setattr(aapi, "embed_text", lambda _text: [0.0, 1.0])

    bg = _BG()
    response = await aapi.create_manual_authored_set(
        body=aapi.ManualAuthoredSetBody(
            search_space_id=space_id,
            problems=[
                aapi.ManualProblemBody(
                    problem_text="Argue whether federalism strengthens accountability.",
                    solution_text="Federalism creates veto points that check power.",
                )
            ],
        ),
        request=_FakeRequest(),
        background=bg,
        db=db_session,
    )
    fn, args, kwargs = bg.tasks[0]
    await fn(*args, **kwargs)

    aset = await db_session.get(AuthoredSet, response["set_id"])
    (problem_result,) = aset.result_summary["problems"]
    return aapi, space_id, int(response["set_id"]), int(problem_result["concept_problem_id"])


def _install_rehoming_stubs(monkeypatch, *, fail: bool = False):
    """Stub the two network-bound re-homing calls. ``fail`` forces a tag failure."""
    import apollo.provisioning.authored_sets.rehoming as rehoming
    from apollo.provisioning.tag_mint import MintPlan, TagMintError

    calls = {"tag_and_mint": 0, "project_canon": 0}

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn, resolved_concept=None, **_kwargs):
        calls["tag_and_mint"] += 1
        if fail:
            raise TagMintError("no concept_slug in tag response")
        row = (
            await db.execute(select(Concept).where(Concept.slug == "provisional.inventory"))
        ).scalar_one()
        return MintPlan(
            concept_id=int(row.id),
            concept_slug="civics-accountability",
            authored_symbols=[],
            minted_entity_ids={},
            merged_entity_keys=[],
            prereq_pairs=[],
            misconception_keys=[],
        )

    async def _project_canon(*_args, **_kwargs):
        calls["project_canon"] += 1

    monkeypatch.setattr(rehoming, "tag_and_mint", _tag_and_mint)
    monkeypatch.setattr(rehoming, "project_canon", _project_canon)
    return calls


@pytest.mark.asyncio
async def test_confirm_promotes_stamps_and_runs_rehoming(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-confirm"
    )
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 1
    assert row.provenance["typed_confirmation"]["status"] == "awaiting_teacher_confirmation"

    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: object())
    calls = _install_rehoming_stubs(monkeypatch)

    bg = _BG()
    resp = await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=bg,
        db=db_session,
    )
    assert resp["promoted"] is True
    assert resp["rehoming"] == "rehoming_pending"
    job_id = resp["job_id"]

    refreshed = await db_session.get(ConceptProblem, problem_id)
    assert refreshed.tier == 2
    # Teacher stamp is ADDED alongside — never replacing — the honesty stamp.
    assert refreshed.payload["teacher_confirmed"] is True
    assert refreshed.payload["teacher_confirmation"]["actor"] == "teacher-1"
    assert refreshed.payload["verification"] == "faithfulness_only"
    assert refreshed.provenance["typed_confirmation"]["status"] == "teacher_confirmed"
    assert refreshed.provenance["typed_rehoming"]["status"] == "rehoming_pending"

    aset = await db_session.get(AuthoredSet, set_id)
    (entry,) = aset.result_summary["problems"]
    assert entry["outcome"] == "promoted"
    assert entry["reason"] == "rehoming_pending"

    job = await db_session.get(RehomingJob, job_id)
    assert job.state == "pending"

    # Run the scheduled durable re-homing job to completion.
    (fn, args, kwargs) = bg.tasks[0]
    await fn(*args, **kwargs)
    done = await db_session.get(ConceptProblem, problem_id)
    assert done.tier == 2  # never demoted
    assert done.provenance["typed_rehoming"]["status"] == "rehoming_complete"
    assert done.payload["concept_id"] == "civics-accountability"
    assert calls == {"tag_and_mint": 1, "project_canon": 1}
    completed = await db_session.get(RehomingJob, job_id)
    assert completed.state == "completed"


@pytest.mark.asyncio
async def test_get_authored_set_exposes_confirmation_and_rehoming(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-poll"
    )
    detail = await aapi.get_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    (entry,) = detail["result_summary"]["problems"]
    confirmation = entry["confirmation"]
    assert confirmation["status"] == "awaiting_teacher_confirmation"
    assert {e["edge_type"] for e in confirmation["draft"]["edges"]} >= {"DEPENDS_ON", "PRECEDES"}
    assert confirmation["draft"]["solution"].startswith("Federalism creates veto points")
    assert entry["rehoming"] is None

    _install_rehoming_stubs(monkeypatch)
    await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    detail2 = await aapi.get_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    (entry2,) = detail2["result_summary"]["problems"]
    assert entry2["rehoming"]["status"] == "rehoming_pending"


@pytest.mark.asyncio
async def test_confirm_requires_course_teacher(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-authz"
    )

    async def _forbidden(**_kwargs):
        raise HTTPException(status_code=403, detail="not a teacher")

    monkeypatch.setattr(aapi, "require_course_teacher", _forbidden)
    with pytest.raises(HTTPException) as excinfo:
        await aapi.confirm_typed_problem(
            set_id=set_id,
            problem_id=problem_id,
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_confirm_conflicts_when_not_awaiting(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-twice"
    )
    _install_rehoming_stubs(monkeypatch)
    await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    with pytest.raises(HTTPException) as excinfo:
        await aapi.confirm_typed_problem(
            set_id=set_id,
            problem_id=problem_id,
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_discard_deletes_pending_draft(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-discard"
    )
    resp = await aapi.discard_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        db=db_session,
    )
    assert resp == {"discarded": True, "problem_id": problem_id}
    assert await db_session.get(ConceptProblem, problem_id) is None
    aset = await db_session.get(AuthoredSet, set_id)
    (entry,) = aset.result_summary["problems"]
    assert entry["outcome"] == "discarded"
    assert aset.status == "done"  # no more pending confirmations


@pytest.mark.asyncio
async def test_edit_resubmit_replaces_pending_draft(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-edit"
    )

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    bg = _BG()
    response = await aapi.create_manual_authored_set(
        body=aapi.ManualAuthoredSetBody(
            search_space_id=space_id,
            replace_problem_id=problem_id,
            problems=[
                aapi.ManualProblemBody(
                    problem_text="Argue whether federalism strengthens accountability (v2).",
                    solution_text="Revised: veto points check power.",
                )
            ],
        ),
        request=_FakeRequest(),
        background=bg,
        db=db_session,
    )
    # The stale pending draft is discarded (edit is a fresh submission).
    assert await db_session.get(ConceptProblem, problem_id) is None
    old_set = await db_session.get(AuthoredSet, set_id)
    (old_entry,) = old_set.result_summary["problems"]
    assert old_entry["outcome"] == "discarded"
    assert old_entry["reason"] == "edit_resubmitted"
    # A brand-new set/draft was created; running it stops at confirmation again.
    fn, args, kwargs = bg.tasks[0]
    await fn(*args, **kwargs)
    new_set = await db_session.get(AuthoredSet, response["set_id"])
    (new_entry,) = new_set.result_summary["problems"]
    assert new_entry["outcome"] == "awaiting_teacher_confirmation"
    assert int(new_entry["concept_problem_id"]) != problem_id


@pytest.mark.asyncio
async def test_indefinite_pending_expiry_stub_is_noop(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-indef"
    )
    # The expiry/reminder hook is a disabled no-op: pending drafts never expire.
    assert aapi.typed_confirmation_expiry_at("2020-01-01T00:00:00+00:00") is None
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.provenance["typed_confirmation"]["status"] == "awaiting_teacher_confirmation"
    _install_rehoming_stubs(monkeypatch)
    resp = await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    assert resp["promoted"] is True  # still actionable regardless of age


@pytest.mark.asyncio
async def test_rehoming_failure_is_queryable_and_retryable(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-rehome-fail"
    )
    calls = _install_rehoming_stubs(monkeypatch, fail=True)
    bg = _BG()
    resp = await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=bg,
        db=db_session,
    )
    assert resp["promoted"] is True  # promotion never blocks on tagging

    fn, args, kwargs = bg.tasks[0]
    await fn(*args, **kwargs)

    failed = await db_session.get(ConceptProblem, problem_id)
    assert failed.tier == 2  # tag failure NEVER un-promotes
    state = failed.provenance["typed_rehoming"]
    assert state["status"] == "rehoming_failed"
    assert "TagMintError" in state["diagnostic"]
    assert calls["tag_and_mint"] == 1

    # The failed job is queryable and (below the retry cap) released for retry.
    job = await db_session.get(RehomingJob, resp["job_id"])
    assert job.state == "pending"
    assert job.last_error and "TagMintError" in job.last_error

    # The retry endpoint enqueues re-homing again on the SAME open job.
    retry = await aapi.retry_typed_rehoming(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    assert retry["rehoming"] == "rehoming_pending"


@pytest.mark.asyncio
async def test_manual_assign_existing_concept_rehomes(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-assign"
    )
    _install_rehoming_stubs(monkeypatch, fail=True)
    await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    # Author a real destination concept the teacher can pick.
    from apollo.persistence.models import Subject

    subject = (
        (await db_session.execute(select(Subject).where(Subject.search_space_id == space_id)))
        .scalars()
        .first()
    )
    target = Concept(
        subject_id=subject.id,
        slug="civics-federalism",
        display_name="Federalism",
        canonical_symbols={},
        normalization_map={},
    )
    db_session.add(target)
    await db_session.flush()

    resp = await aapi.assign_typed_problem_concept(
        set_id=set_id,
        problem_id=problem_id,
        body=aapi.AssignConceptBody(concept_id=int(target.id)),
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    assert resp["rehoming"] == "rehoming_pending"
    job = await db_session.get(RehomingJob, resp["job_id"])
    assert int(job.requested_concept_id) == int(target.id)

    # A non-existent concept is a 404, not a silent enqueue.
    with pytest.raises(HTTPException) as excinfo:
        await aapi.assign_typed_problem_concept(
            set_id=set_id,
            problem_id=problem_id,
            body=aapi.AssignConceptBody(concept_id=999999),
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert excinfo.value.status_code == 404


# --------------------------------------------------------------------------- #
# confirm's NOT-promoted branch: held-for-review and rejected outcomes never
# reach re-homing, and a rejection also writes the RejectedProblem ledger row.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirm_held_for_review_records_ledger_without_rehoming(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-held"
    )

    async def _held(
        db, *, problem, concept_problem_id, existing_problem_hashes, confirmed_by, confirmed_at
    ):
        return PromoteHeldForReview(promoted=False, failed_gate=9, diagnostic="gate 9: unresolved")

    monkeypatch.setattr(aapi, "promote_typed_confirmed", _held)
    resp = await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    assert resp == {
        "promoted": False,
        "outcome": "held_for_review",
        "failed_gate": 9,
        "diagnostic": "gate 9: unresolved",
    }
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 1  # never promoted
    assert row.provenance["typed_confirmation"]["status"] == "teacher_confirmed_not_promoted"
    assert row.provenance["typed_confirmation"]["diagnostic"] == "gate 9: unresolved"
    aset = await db_session.get(AuthoredSet, set_id)
    (entry,) = aset.result_summary["problems"]
    assert entry["outcome"] == "held_for_review"
    assert entry["reason"] == "solve_unresolved"
    # held-for-review is not a rejection: no RejectedProblem ledger row.
    rejected = (await db_session.execute(select(RejectedProblem))).scalars().all()
    assert rejected == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_gate,expected_reason,expected_payload_reason",
    [(8, "duplicate", "duplicate"), (9, "solve_refuted", "solve_and_check")],
)
async def test_confirm_rejected_records_rejection_and_increments_run(
    db_session, monkeypatch, failed_gate, expected_reason, expected_payload_reason
):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug=f"typed-rejected-{failed_gate}"
    )

    async def _rejected(
        db, *, problem, concept_problem_id, existing_problem_hashes, confirmed_by, confirmed_at
    ):
        return PromoteResult(promoted=False, failed_gate=failed_gate, diagnostic="gate refuted")

    monkeypatch.setattr(aapi, "promote_typed_confirmed", _rejected)
    resp = await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    assert resp == {
        "promoted": False,
        "outcome": "rejected",
        "failed_gate": failed_gate,
        "diagnostic": "gate refuted",
    }
    row = await db_session.get(ConceptProblem, problem_id)
    assert row.tier == 1
    assert row.provenance["typed_confirmation"]["status"] == "teacher_confirmed_not_promoted"
    aset = await db_session.get(AuthoredSet, set_id)
    (entry,) = aset.result_summary["problems"]
    assert entry["outcome"] == "rejected"
    assert entry["reason"] == expected_reason

    (rejected_row,) = (await db_session.execute(select(RejectedProblem))).scalars().all()
    assert rejected_row.failed_gate == failed_gate
    assert rejected_row.rejected_stage == "typed_confirmation_promotion"
    assert rejected_row.payload["reason"] == expected_payload_reason

    ingest_run_id = aset.result_summary.get("ingest_run_id")
    assert isinstance(ingest_run_id, int)
    run = await db_session.get(IngestRun, ingest_run_id)
    assert run.n_rejected == 1


# --------------------------------------------------------------------------- #
# 409 guards on the other typed-path endpoints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_edit_pending_typed_draft_returns_409(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-edit-409"
    )
    with pytest.raises(HTTPException) as excinfo:
        await aapi.edit_authored_problem(
            concept_problem_id=problem_id,
            body=aapi.ProblemEditBody(problem_text="Updated question text"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_discard_conflicts_when_not_awaiting(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-discard-409"
    )
    _install_rehoming_stubs(monkeypatch)
    await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    with pytest.raises(HTTPException) as excinfo:
        await aapi.discard_typed_problem(
            set_id=set_id,
            problem_id=problem_id,
            request=_FakeRequest(),
            db=db_session,
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_retry_rehoming_requires_tier_2(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-retry-409"
    )
    with pytest.raises(HTTPException) as excinfo:
        await aapi.retry_typed_rehoming(
            set_id=set_id,
            problem_id=problem_id,
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_edit_resubmit_409_when_replace_target_missing(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-replace-409"
    )

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    with pytest.raises(HTTPException) as excinfo:
        await aapi.create_manual_authored_set(
            body=aapi.ManualAuthoredSetBody(
                search_space_id=space_id,
                replace_problem_id=999999,
                problems=[
                    aapi.ManualProblemBody(problem_text="New question", solution_text="New answer")
                ],
            ),
            request=_FakeRequest(),
            background=_BG(),
            db=db_session,
        )
    assert excinfo.value.status_code == 409


# --------------------------------------------------------------------------- #
# Private helpers exercised directly: _replace_result_entry, _typed_problem_
# hashes, _typed_set_problem, _run_rehoming_job_background.
# --------------------------------------------------------------------------- #


def test_replace_result_entry_returns_false_when_no_problems_list():
    import apollo.provisioning.authored_sets.api as aapi

    aset = AuthoredSet(search_space_id=1, set_index=1, status="pending", result_summary={})
    assert aapi._replace_result_entry(aset, problem_id=1, outcome="discarded", reason="x") is False


def test_replace_result_entry_skips_non_matching_and_returns_false_when_missing():
    import apollo.provisioning.authored_sets.api as aapi

    aset = AuthoredSet(
        search_space_id=1,
        set_index=1,
        status="provisioning",
        result_summary={
            "problems": [
                {"concept_problem_id": 1, "outcome": "awaiting_teacher_confirmation"},
                {"concept_problem_id": 2, "outcome": "awaiting_teacher_confirmation"},
            ]
        },
    )
    # No entry matches problem_id=999: the loop passes every entry through
    # unchanged and the function reports no replacement happened.
    replaced = aapi._replace_result_entry(aset, problem_id=999, outcome="discarded", reason="x")
    assert replaced is False
    assert [p["concept_problem_id"] for p in aset.result_summary["problems"]] == [1, 2]


@pytest.mark.asyncio
async def test_replace_problem_result_returns_none_when_problem_in_no_set(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-replace-none"
    )
    result = await aapi._replace_problem_result(
        db_session, problem_id=999999, outcome="discarded", reason="x"
    )
    assert result is None


@pytest.mark.asyncio
async def test_typed_problem_hashes_skips_invalid_payloads(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-hashes"
    )
    _install_rehoming_stubs(monkeypatch)
    await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )
    good_row = await db_session.get(ConceptProblem, problem_id)
    assert good_row.tier == 2

    bad = ConceptProblem(
        concept_id=good_row.concept_id,
        problem_code="authored.malformed",
        difficulty="intro",
        payload={"not": "a valid problem"},
        tier=2,
        solution_source="authored",
        provenance={},
        search_space_id=space_id,
    )
    db_session.add(bad)
    await db_session.flush()

    hashes = await aapi._typed_problem_hashes(
        db_session, search_space_id=space_id, exclude_problem_id=999999
    )
    assert len(hashes) == 1  # the malformed payload is skipped, not raised


@pytest.mark.asyncio
async def test_typed_set_problem_404_when_set_missing(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-set-404a"
    )
    with pytest.raises(HTTPException) as excinfo:
        await aapi._typed_set_problem(
            db_session, set_id=999999, problem_id=problem_id, auth=object()
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_typed_set_problem_404_when_problem_not_in_set(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-set-404b"
    )
    with pytest.raises(HTTPException) as excinfo:
        await aapi._typed_set_problem(db_session, set_id=set_id, problem_id=999999, auth=object())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_run_rehoming_job_background_returns_when_nothing_claimable(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-bg-none"
    )

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)

    async def _claim_none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(aapi, "claim_rehoming_job", _claim_none)

    # Must return quietly with nothing claimed — no exception, no further calls.
    assert await aapi._run_rehoming_job_background(job_id=999999) is None


@pytest.mark.asyncio
async def test_run_rehoming_job_background_resolves_requested_concept(db_session, monkeypatch):
    aapi, space_id, set_id, problem_id = await _make_pending_draft(
        db_session, monkeypatch, slug="typed-bg-resolve"
    )
    _install_rehoming_stubs(monkeypatch)
    await aapi.confirm_typed_problem(
        set_id=set_id,
        problem_id=problem_id,
        request=_FakeRequest(),
        background=_BG(),
        db=db_session,
    )

    from apollo.persistence.models import Subject

    subject = (
        (await db_session.execute(select(Subject).where(Subject.search_space_id == space_id)))
        .scalars()
        .first()
    )
    target = Concept(
        subject_id=subject.id,
        slug="civics-assigned",
        display_name="Assigned",
        canonical_symbols={},
        normalization_map={},
    )
    db_session.add(target)
    await db_session.flush()
    target_id = int(target.id)
    await db_session.commit()

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)

    claimed = ClaimedRehoming(
        job_id=1,
        problem_id=problem_id,
        search_space_id=space_id,
        requested_concept_id=target_id,
        attempt_count=1,
    )

    async def _claim(*_args, **_kwargs):
        return claimed

    monkeypatch.setattr(aapi, "claim_rehoming_job", _claim)

    seen = {}

    async def _run_rehoming(
        db, neo, *, problem_id, chat_fn, embed_fn, resolved_concept=None, job_id=None
    ):
        seen["resolved_concept"] = resolved_concept
        return True

    monkeypatch.setattr(aapi, "run_rehoming", _run_rehoming)

    completed = {"called": False}

    async def _complete(db, *, job_id):
        completed["called"] = True

    monkeypatch.setattr(aapi, "complete_rehoming_job", _complete)

    await aapi._run_rehoming_job_background(job_id=1)
    assert seen["resolved_concept"].concept_id == target_id
    assert seen["resolved_concept"].slug == "civics-assigned"
    assert completed["called"] is True
