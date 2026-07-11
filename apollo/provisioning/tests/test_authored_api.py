from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from auth import AuthContext


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


class _FakeRequest:
    pass


class _FakeUpload:
    def __init__(self, body: bytes, filename: str):
        self._body = body
        self.filename = filename

    async def read(self) -> bytes:
        return self._body


async def _fake_require_user(_request):
    return AuthContext(user_id="teacher-1", access_token="token")


async def _fake_require_member(**_kwargs):
    return None


class _FakeNeoResult:
    """Async-iterable / single()-able stand-in for a neo4j Result."""

    def __init__(self, records):
        self._records = list(records)

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for rec in self._records:
            yield rec

    async def single(self):
        return self._records[0] if self._records else None


class _FakeNeoSession:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def run(self, cypher, **params):
        self._client.calls.append((cypher, params))
        if "RETURN DISTINCT cid" in cypher:  # the :Canon student-history read
            cids = params.get("cids", [])
            recs = [{"cid": c} for c in cids if c in self._client.with_history]
        else:
            recs = []
        return _FakeNeoResult(recs)


class _FakeNeo:
    """Records every Cypher run; reports RESOLVES_TO history for `with_history`."""

    def __init__(self, with_history=None):
        self.calls = []
        self.with_history = set(with_history or [])

    def session(self):
        return _FakeNeoSession(self)


async def _seed_course(db, *, slug: str = "aas-api") -> tuple[int, int]:
    from apollo.persistence.models import Concept, Subject
    from database.models import SearchSpace

    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subject = Subject(slug=f"subject-{slug}", display_name="Physics", search_space_id=space.id)
    db.add(subject)
    await db.flush()
    concept = Concept(
        subject_id=subject.id,
        slug=f"concept-{slug}",
        display_name="Concept",
        canonical_symbols={},
        normalization_map={},
    )
    db.add(concept)
    await db.flush()
    return int(space.id), int(concept.id)


@pytest.mark.asyncio
async def test_create_set_persists_and_schedules(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    search_space_id, _concept_id = await _seed_course(db_session, slug="create")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    bg = _BG()

    resp = await aapi.create_authored_set(
        request=_FakeRequest(),
        background=bg,
        problem=_FakeUpload(b"%PDF p", "problems.pdf"),
        solution=_FakeUpload(b"%PDF s", "solutions.pdf"),
        search_space_id=search_space_id,
        db=db_session,
    )

    assert resp["status"] == "pending"
    assert resp["set_index"] == 1
    assert len(bg.tasks) == 1

    from apollo.persistence.models import AuthoredSet

    row = await db_session.get(AuthoredSet, resp["set_id"])
    assert row.search_space_id == search_space_id
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_create_set_without_solution_schedules_background(db_session, monkeypatch):
    """B1: the solution PDF is optional — a POST with no solution part must still
    schedule the background task, passing ``solution_bytes=None``."""
    import apollo.provisioning.authored_sets.api as aapi

    search_space_id, _concept_id = await _seed_course(db_session, slug="create-nosol")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    bg = _BG()

    resp = await aapi.create_authored_set(
        request=_FakeRequest(),
        background=bg,
        problem=_FakeUpload(b"%PDF p", "problems.pdf"),
        solution=None,
        search_space_id=search_space_id,
        db=db_session,
    )

    assert resp["status"] == "pending"
    assert len(bg.tasks) == 1
    _fn, _args, kwargs = bg.tasks[0]
    assert kwargs["solution_bytes"] is None
    assert kwargs["solution_title"] is None


def test_get_neo4j_client_delegates(monkeypatch):
    import apollo.api as apollo_api
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(apollo_api, "get_neo4j_client", lambda: "neo-client")
    assert aapi.get_neo4j_client() == "neo-client"


@pytest.mark.asyncio
async def test_background_runner_returns_when_row_vanishes(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    # A real search space so the ingest-run row (opened before the vanish check)
    # satisfies its search_space_id FK; the AuthoredSet id stays nonexistent.
    search_space_id, _concept_id = await _seed_course(db_session, slug="vanish")

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, **_kwargs):
        return 101 if role == "problem" else 102

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "MeteredChat", lambda **_kwargs: "metered")
    # No AuthoredSet row for this id -> the post-index fetch is None -> early return.
    await aapi._run_set_background(
        set_id=999999,
        search_space_id=search_space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="p",
        solution_bytes=b"%PDF s",
        solution_title="s",
    )


@pytest.mark.asyncio
async def test_background_runner_persists_failure(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    search_space_id, _concept_id = await _seed_course(db_session, slug="bgfail")
    row = AuthoredSet(search_space_id=search_space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_fail(db, **_kwargs):
        raise RuntimeError("index boom")

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_fail)

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=search_space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="p",
        solution_bytes=b"%PDF s",
        solution_title="s",
    )

    refreshed = await db_session.get(AuthoredSet, set_id)
    assert refreshed.status == "failed"
    assert "index boom" in refreshed.result_summary["error"]


@pytest.mark.asyncio
async def test_get_authored_set_404(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    with pytest.raises(HTTPException) as exc:
        await aapi.get_authored_set(set_id=999999, request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approve_404_when_set_missing(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=999999,
            problem_id=1,
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approve_409_when_not_held(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    space, concept = await _seed_course(db_session, slug="approve409")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    prob = ConceptProblem(
        concept_id=concept,
        problem_code="not-held",
        difficulty="intro",
        tier=1,
        payload={},
        search_space_id=space,
        provenance={},  # no authored_review -> not held
    )
    db_session.add(prob)
    await db_session.flush()
    aset = AuthoredSet(
        search_space_id=space,
        set_index=1,
        status="done",
        result_summary={
            "problems": [{"concept_problem_id": prob.id, "outcome": "held_for_review"}]
        },
    )
    db_session.add(aset)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(aset.id),
            problem_id=int(prob.id),
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_approve_422_when_chosen_reference_missing(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    space, concept = await _seed_course(db_session, slug="approve422")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    prob = ConceptProblem(
        concept_id=concept,
        problem_code="held-no-ocr",
        difficulty="intro",
        tier=1,
        payload={},
        search_space_id=space,
        provenance={
            "authored_review": {"required": True, "ocr_draft": None, "generated_alt": None}
        },
    )
    db_session.add(prob)
    await db_session.flush()
    aset = AuthoredSet(
        search_space_id=space,
        set_index=1,
        status="done",
        result_summary={
            "problems": [{"concept_problem_id": prob.id, "outcome": "held_for_review"}]
        },
    )
    db_session.add(aset)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(aset.id),
            problem_id=int(prob.id),
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_approve_404_when_problem_not_minted_by_this_set(db_session, monkeypatch):
    """C1 regression: a real ConceptProblem in the SAME course, but not recorded in
    this set's result_summary (e.g. it belongs to a sibling set), must 404 rather
    than promote under the caller's search space."""
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    space, concept = await _seed_course(db_session, slug="approve404cross")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    other_set = AuthoredSet(search_space_id=space, set_index=1, status="done")
    db_session.add(other_set)
    prob = ConceptProblem(
        concept_id=concept,
        problem_code="held-cross-set",
        difficulty="intro",
        tier=1,
        payload={},
        search_space_id=space,
        provenance={
            "authored_review": {"required": True, "ocr_draft": {"x": 1}, "generated_alt": None}
        },
    )
    db_session.add(prob)
    await db_session.flush()
    # This set never minted `prob` -- its own result_summary is empty.
    aset = AuthoredSet(search_space_id=space, set_index=2, status="done", result_summary={})
    db_session.add(aset)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(aset.id),
            problem_id=int(prob.id),
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approve_404_when_problem_in_different_search_space(db_session, monkeypatch):
    """C1 regression: even if a corrupted/stale result_summary lists a
    concept_problem_id, a search-space mismatch between the problem and the set
    must still 404 -- the strict cross-tenant guard."""
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    space_a, concept_a = await _seed_course(db_session, slug="approve404space-a")
    space_b, _concept_b = await _seed_course(db_session, slug="approve404space-b")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    # Problem actually lives in course A.
    prob = ConceptProblem(
        concept_id=concept_a,
        problem_code="held-other-space",
        difficulty="intro",
        tier=1,
        payload={},
        search_space_id=space_a,
        provenance={
            "authored_review": {"required": True, "ocr_draft": {"x": 1}, "generated_alt": None}
        },
    )
    db_session.add(prob)
    await db_session.flush()
    # But the (attacker-controlled) set being approved through is in course B, and
    # its result_summary falsely claims this problem as its own.
    aset = AuthoredSet(
        search_space_id=space_b,
        set_index=1,
        status="done",
        result_summary={
            "problems": [{"concept_problem_id": prob.id, "outcome": "held_for_review"}]
        },
    )
    db_session.add(aset)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(aset.id),
            problem_id=int(prob.id),
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approve_404_when_problem_id_nonexistent(db_session, monkeypatch):
    """A real (existing) set with a nonexistent problem_id 404s -- same status as
    the cross-tenant cases, so existence of the set is never leaked either way."""
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space, _concept = await _seed_course(db_session, slug="approve404missing")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    aset = AuthoredSet(search_space_id=space, set_index=1, status="done", result_summary={})
    db_session.add(aset)
    await db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(aset.id),
            problem_id=999999,
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_background_runner_indexes_and_persists_report(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet
    from apollo.provisioning.authored_sets.orchestrator import ProvisioningReport

    search_space_id, _concept_id = await _seed_course(db_session, slug="background")
    row = AuthoredSet(search_space_id=search_space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, **_kwargs):
        assert db is db_session
        return 101 if role == "problem" else 102

    async def _run_provisioning(db, neo, **kwargs):
        assert db is db_session
        assert neo == "neo"
        assert kwargs["search_space_id"] == search_space_id
        assert kwargs["problem_document_id"] == 101
        assert kwargs["solution_document_id"] == 102
        return ProvisioningReport(problems=[], counts={"promoted": 0})

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "run_authored_set_provisioning", _run_provisioning)
    monkeypatch.setattr(aapi, "MeteredChat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=search_space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="problems.pdf",
        solution_bytes=b"%PDF s",
        solution_title="solutions.pdf",
    )

    refreshed = await db_session.get(AuthoredSet, set_id)
    assert refreshed.problem_document_id == 101
    assert refreshed.solution_document_id == 102
    assert refreshed.status == "done"
    assert refreshed.result_summary["counts"] == {"promoted": 0}


@pytest.mark.asyncio
async def test_background_runner_without_solution_leaves_solution_document_id_null(
    db_session, monkeypatch
):
    """B1: ``solution_bytes=None`` (no solution PDF uploaded) must skip
    solution-role indexing entirely, persist a NULL ``solution_document_id``,
    and hand provisioning ``solution_document_id=None``."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet
    from apollo.provisioning.authored_sets.orchestrator import ProvisioningReport

    search_space_id, _concept_id = await _seed_course(db_session, slug="background-nosol")
    row = AuthoredSet(search_space_id=search_space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    indexed_roles: list[str] = []

    async def _index_authored_doc(db, *, role, **_kwargs):
        indexed_roles.append(role)
        assert role == "problem"  # solution indexing must be skipped entirely
        return 101

    async def _run_provisioning(db, neo, **kwargs):
        assert kwargs["solution_document_id"] is None
        return ProvisioningReport(problems=[], counts={"promoted": 0})

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "run_authored_set_provisioning", _run_provisioning)
    monkeypatch.setattr(aapi, "MeteredChat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=search_space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="problems.pdf",
        solution_bytes=None,
        solution_title=None,
    )

    assert indexed_roles == ["problem"]
    refreshed = await db_session.get(AuthoredSet, set_id)
    assert refreshed.problem_document_id == 101
    assert refreshed.solution_document_id is None
    assert refreshed.status == "done"


@pytest.mark.asyncio
async def test_background_runner_same_doc_guard_treats_solution_as_absent(db_session, monkeypatch):
    """B2: a solution upload whose content_hash matches the problem doc's (the
    teacher uploaded the SAME file for both roles) must be treated as absent —
    NULL ``solution_document_id``, a structured warning log, and a note in
    ``result_summary`` — instead of grounding questions against their own prose."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet
    from apollo.provisioning.authored_sets.orchestrator import ProvisioningReport

    search_space_id, _concept_id = await _seed_course(db_session, slug="background-samedoc")
    row = AuthoredSet(search_space_id=search_space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, **_kwargs):
        return 101 if role == "problem" else 102

    async def _doc_content_hash(db, document_id):
        # Both docs hash identically -- the same PDF uploaded as both roles.
        return "identical-hash"

    async def _run_provisioning(db, neo, **kwargs):
        assert kwargs["solution_document_id"] is None
        return ProvisioningReport(problems=[], counts={"promoted": 0})

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "_doc_content_hash", _doc_content_hash)
    monkeypatch.setattr(aapi, "run_authored_set_provisioning", _run_provisioning)
    monkeypatch.setattr(aapi, "MeteredChat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=search_space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="problems.pdf",
        solution_bytes=b"%PDF p",  # same bytes as the problem doc
        solution_title="problems.pdf",
    )

    refreshed = await db_session.get(AuthoredSet, set_id)
    assert refreshed.problem_document_id == 101
    assert refreshed.solution_document_id is None
    assert refreshed.status == "done"
    assert "same_doc_solution_guard" in refreshed.result_summary
    assert "identical content" in refreshed.result_summary["same_doc_solution_guard"]


@pytest.mark.asyncio
async def test_get_authored_set_surfaces_rejected_and_held_for_review(db_session, monkeypatch):
    """B4: the per-set GET must pass through rejected AND held_for_review
    candidates (with their outcome + diagnostic) from ``result_summary`` — the
    review queue the teacher UI renders. Verifies the existing passthrough
    (``result_summary`` is returned verbatim) already carries this."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    search_space_id, _concept_id = await _seed_course(db_session, slug="get-review-queue")
    row = AuthoredSet(
        search_space_id=search_space_id,
        set_index=1,
        status="done",
        result_summary={
            "problems": [
                {
                    "concept_problem_id": 1,
                    "outcome": "promoted",
                },
                {
                    "concept_problem_id": 2,
                    "outcome": "held_for_review",
                    "reason": "generated_no_match",
                    "diagnostic": "",
                },
                {
                    "concept_problem_id": None,
                    "outcome": "rejected",
                    "diagnostic": "pairing_gate: not faithful to grounding",
                },
            ],
            "counts": {"promoted": 1, "rejected": 1, "held_for_review": 1},
        },
    )
    db_session.add(row)
    await db_session.flush()

    detail = await aapi.get_authored_set(set_id=int(row.id), request=_FakeRequest(), db=db_session)

    problems = detail["result_summary"]["problems"]
    held = next(p for p in problems if p["outcome"] == "held_for_review")
    rejected = next(p for p in problems if p["outcome"] == "rejected")
    assert held["reason"] == "generated_no_match"
    assert rejected["diagnostic"] == "pairing_gate: not faithful to grounding"


@pytest.mark.asyncio
async def test_list_and_detail_are_course_gated(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    search_space_id, _concept_id = await _seed_course(db_session, slug="list")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    seen = []

    async def _member(**kwargs):
        seen.append(kwargs["search_space_id"])

    monkeypatch.setattr(aapi, "require_course_teacher", _member)
    row = AuthoredSet(
        search_space_id=search_space_id,
        set_index=1,
        status="done",
        problem_document_id=101,
        solution_document_id=102,
        result_summary={"counts": {"promoted": 1}},
    )
    db_session.add(row)
    await db_session.flush()

    listed = await aapi.list_authored_sets(
        request=_FakeRequest(), search_space_id=search_space_id, db=db_session
    )
    detail = await aapi.get_authored_set(set_id=int(row.id), request=_FakeRequest(), db=db_session)

    assert [s["set_id"] for s in listed["sets"]] == [int(row.id)]
    assert detail["result_summary"] == {"counts": {"promoted": 1}}
    assert seen == [search_space_id, search_space_id]


@pytest.mark.asyncio
async def test_approve_held_problem_promotes_chosen_reference(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem
    from apollo.provisioning.promote import PromoteResult
    from apollo.provisioning.tag_mint import MintPlan

    search_space_id, concept_id = await _seed_course(db_session, slug="approve")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    problem = ConceptProblem(
        concept_id=concept_id,
        problem_code="scrape.approve",
        difficulty="intro",
        tier=1,
        payload={
            "problem_text": "Find M.",
            "given_values": {},
            "target_unknown": "M",
            "concept_slug": "provisional.inventory",
            "label": "1",
        },
        search_space_id=search_space_id,
        provenance={
            "chunk_content_hash": "approve",
            "authored_review": {
                "required": True,
                "ocr_draft": {
                    "solution_source": "extracted",
                    "reference_solution": [
                        {
                            "step": 1,
                            "entry_type": "equation",
                            "id": "eq1",
                            "content": {"symbolic": "M"},
                            "depends_on": [],
                        }
                    ],
                    "grounding": [],
                    "provenance": {},
                },
                "generated_alt": None,
            },
        },
    )
    db_session.add(problem)
    await db_session.flush()
    aset = AuthoredSet(
        search_space_id=search_space_id,
        set_index=1,
        status="done",
        result_summary={
            "problems": [{"concept_problem_id": problem.id, "outcome": "held_for_review"}]
        },
    )
    db_session.add(aset)
    await db_session.flush()

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn):
        assert pair.solution_source == "extracted"
        assert pair.problem["id"] == "scrape.approve"
        return MintPlan(
            concept_id=concept_id,
            concept_slug="provisional.inventory",
            authored_symbols=[],
            minted_entity_ids={},
            merged_entity_keys=[],
            prereq_pairs=[],
            misconception_keys=[],
        )

    async def _promote(db, neo, **kwargs):
        assert neo == "neo"
        assert kwargs["concept_problem_id"] == int(problem.id)
        # The teacher-approved EXTRACTED OCR draft's provenance is threaded into
        # promote so the re-promoted row records "extracted", not "generated".
        assert kwargs["solution_source"] == "extracted"
        row = await db.get(ConceptProblem, kwargs["concept_problem_id"])
        row.tier = 2
        return PromoteResult(promoted=True)

    monkeypatch.setattr(aapi, "MeteredChat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "tag_and_mint", _tag_and_mint)
    monkeypatch.setattr(aapi, "promote", _promote)
    monkeypatch.setattr(aapi, "embed_text", lambda _text: [0.0])
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    async def _dup_hashes(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(aapi, "_authored_concept_dup_hashes", _dup_hashes)

    resp = await aapi.approve_held_problem(
        set_id=int(aset.id),
        problem_id=int(problem.id),
        body=aapi.ApproveBody(reference="ocr"),
        request=_FakeRequest(),
        db=db_session,
    )

    assert resp == {"promoted": True, "failed_gate": None, "diagnostic": ""}
    refreshed = await db_session.get(ConceptProblem, problem.id)
    review = refreshed.provenance["authored_review"]
    assert review["required"] is False
    assert review["approved_reference"] == "ocr"


def test_make_metered_chat_seeds_decimal_cost_not_float():
    """Regression: the authored-set ingest_run stub seeded ``llm_cost_usd`` as a
    float ``0.0``, so the first metered LLM call hit
    ``float += Decimal`` -> TypeError and failed the whole run. The seed must be
    Decimal so ``record_usage`` accumulates ``cost_usd_for``'s Decimal cleanly."""
    from decimal import Decimal
    from types import SimpleNamespace

    import apollo.provisioning.authored_sets.api as aapi

    chat = aapi._make_metered_chat(document_id=42)
    assert isinstance(chat._run.llm_cost_usd, Decimal)

    # Accumulating a real (Decimal) cost must not raise.
    usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    chat.record_usage(model="gpt-4o", usage=usage)
    assert isinstance(chat._run.llm_cost_usd, Decimal)
    assert chat._run.llm_cost_usd > 0


@pytest.mark.asyncio
async def test_delete_authored_set_cascades_and_spares_siblings(db_session, monkeypatch):
    """Delete removes the set, its reference docs (+chunks), and the
    ConceptProblems it minted — while a sibling set's problem is untouched."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem
    from database.models import AITAChunk, AITADocument

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)

    space_id, concept_id = await _seed_course(db_session, slug="aas-del")

    # Two reference docs for the target set, each with a chunk.
    pdoc = AITADocument(
        title="p",
        content="c",
        content_hash="ph",
        unique_identifier_hash="pu",
        search_space_id=space_id,
        status={"state": "apollo_reference"},
    )
    sdoc = AITADocument(
        title="s",
        content="c",
        content_hash="sh",
        unique_identifier_hash="su",
        search_space_id=space_id,
        status={"state": "apollo_reference"},
    )
    db_session.add_all([pdoc, sdoc])
    await db_session.flush()
    db_session.add_all(
        [
            AITAChunk(document_id=pdoc.id, content="pc"),
            AITAChunk(document_id=sdoc.id, content="sc"),
        ]
    )

    # Two ConceptProblems minted by the target set, one by a sibling set.
    cp1 = ConceptProblem(
        concept_id=concept_id,
        problem_code="scrape.a",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space_id,
    )
    cp2 = ConceptProblem(
        concept_id=concept_id,
        problem_code="scrape.b",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space_id,
    )
    sibling = ConceptProblem(
        concept_id=concept_id,
        problem_code="scrape.z",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space_id,
    )
    db_session.add_all([cp1, cp2, sibling])
    await db_session.flush()

    target = AuthoredSet(
        search_space_id=space_id,
        set_index=1,
        status="done",
        problem_document_id=pdoc.id,
        solution_document_id=sdoc.id,
        result_summary={
            "problems": [
                {"concept_problem_id": cp1.id, "outcome": "promoted"},
                {"concept_problem_id": cp2.id, "outcome": "held_for_review"},
                {"concept_problem_id": None, "outcome": "rejected"},
            ]
        },
    )
    db_session.add(target)
    await db_session.flush()
    target_id, pdoc_id, sdoc_id = int(target.id), int(pdoc.id), int(sdoc.id)
    cp1_id, cp2_id, sibling_id = int(cp1.id), int(cp2.id), int(sibling.id)

    resp = await aapi.delete_authored_set(set_id=target_id, request=_FakeRequest(), db=db_session)
    assert resp["deleted"] is True
    assert resp["removed_problems"] == 2
    assert resp["removed_documents"] == 2

    assert await db_session.get(AuthoredSet, target_id) is None
    assert await db_session.get(AITADocument, pdoc_id) is None
    assert await db_session.get(AITADocument, sdoc_id) is None
    assert await db_session.get(ConceptProblem, cp1_id) is None
    assert await db_session.get(ConceptProblem, cp2_id) is None
    # Chunks cascade with their document.
    remaining_chunks = (
        await db_session.execute(
            select(func.count())
            .select_from(AITAChunk)
            .where(AITAChunk.document_id.in_([pdoc_id, sdoc_id]))
        )
    ).scalar_one()
    assert remaining_chunks == 0
    # Sibling problem survives.
    assert await db_session.get(ConceptProblem, sibling_id) is not None


@pytest.mark.asyncio
async def test_delete_failed_set_with_no_problems(db_session, monkeypatch):
    """A failed run (error summary, no problems, no doc ids) deletes cleanly —
    the core motivation: clearing failed sets off the console."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space_id, _ = await _seed_course(db_session, slug="aas-del-failed")

    row = AuthoredSet(
        search_space_id=space_id, set_index=1, status="failed", result_summary={"error": "boom"}
    )
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp == {
        "deleted": True,
        "removed_problems": 0,
        "removed_documents": 0,
        "removed_concepts": 0,
    }
    assert await db_session.get(AuthoredSet, set_id) is None


@pytest.mark.asyncio
async def test_delete_authored_set_404(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.delete_authored_set(set_id=999999, request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "indexing", "provisioning"])
async def test_delete_authored_set_409_while_in_flight(db_session, monkeypatch, status):
    """H2 regression: deleting a set mid-provisioning would orphan :Canon nodes
    the background task writes outside the PG transaction -- reject with 409
    while the run is still in flight."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space_id, _ = await _seed_course(db_session, slug=f"aas-inflight-{status}")
    row = AuthoredSet(search_space_id=space_id, set_index=1, status=status)
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 409
    # The set survives the rejected delete.
    assert await db_session.get(AuthoredSet, set_id) is not None


@pytest.mark.asyncio
async def test_delete_authored_set_enforces_membership(db_session, monkeypatch):
    """A non-member is rejected and the set survives."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    async def _deny_member(**_kwargs):
        raise aapi.HTTPException(status_code=403, detail="not a member")

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _deny_member)
    space_id, _ = await _seed_course(db_session, slug="aas-del-auth")
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 403
    assert await db_session.get(AuthoredSet, set_id) is not None


async def _seed_orphanable_concept_kg(db, monkeypatch, *, slug):
    """Seed a course + a concept whose ONLY ConceptProblem belongs to one authored
    set, plus the KG the set minted (two entities, a prereq edge, a dedup decision).
    Returns (aapi, space_id, concept_id, entity_ids, set_id)."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import (
        AuthoredSet,
        ConceptProblem,
        DedupDecision,
        EntityPrereq,
        KGEntity,
    )

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space_id, concept_id = await _seed_course(db, slug=slug)

    e1 = KGEntity(
        concept_id=concept_id,
        canonical_key="eq.a",
        kind="equation",
        display_name="a",
        payload={},
        aliases=[],
    )
    e2 = KGEntity(
        concept_id=concept_id,
        canonical_key="proc.b",
        kind="procedure",
        display_name="b",
        payload={},
        aliases=[],
    )
    db.add_all([e1, e2])
    await db.flush()
    db.add(EntityPrereq(from_entity_id=e2.id, to_entity_id=e1.id))
    db.add(
        DedupDecision(
            search_space_id=space_id,
            concept_id=concept_id,
            candidate_key="eq.a",
            method="slug",
            similarity=None,
            verdict="distinct",
        )
    )
    cp = ConceptProblem(
        concept_id=concept_id,
        problem_code="scrape.a",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space_id,
    )
    db.add(cp)
    await db.flush()
    entity_ids = (int(e1.id), int(e2.id))

    target = AuthoredSet(
        search_space_id=space_id,
        set_index=1,
        status="done",
        result_summary={"problems": [{"concept_problem_id": cp.id, "outcome": "promoted"}]},
    )
    db.add(target)
    await db.flush()
    return aapi, space_id, concept_id, entity_ids, int(target.id)


@pytest.mark.asyncio
async def test_delete_authored_set_tears_down_orphaned_concept(db_session, monkeypatch):
    """When the deleted set's problems were the ONLY ones on a concept and there is
    no :Canon student history, the concept's full KG is torn down: apollo_concepts
    (cascading KGEntity + apollo_entity_prereqs) + apollo_dedup_decisions in
    Postgres, and its :Canon nodes are DETACH DELETEd (guarded) in Neo4j."""
    from apollo.persistence.models import Concept, DedupDecision, EntityPrereq, KGEntity

    aapi, _space_id, concept_id, (e1_id, e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-orphan"
    )
    fake_neo = _FakeNeo()  # no RESOLVES_TO history for any concept
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: fake_neo)

    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp["removed_concepts"] == 1

    assert await db_session.get(Concept, concept_id) is None
    assert await db_session.get(KGEntity, e1_id) is None
    assert await db_session.get(KGEntity, e2_id) is None
    remaining_prereqs = (
        await db_session.execute(
            select(func.count())
            .select_from(EntityPrereq)
            .where(EntityPrereq.from_entity_id == e2_id)
        )
    ).scalar_one()
    assert remaining_prereqs == 0
    remaining_dedup = (
        await db_session.execute(
            select(func.count())
            .select_from(DedupDecision)
            .where(DedupDecision.concept_id == concept_id)
        )
    ).scalar_one()
    assert remaining_dedup == 0
    # A guarded :Canon DETACH DELETE was issued for this concept.
    assert any(
        "DETACH DELETE" in cypher and concept_id in params.get("cids", [])
        for cypher, params in fake_neo.calls
    )


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_student_history(db_session, monkeypatch):
    """A concept whose :Canon carries RESOLVES_TO student history is NOT torn down,
    even when the deleted set's problems were its only problems — grading history is
    never destroyed. No PG concept/KG delete, no Neo4j DETACH DELETE."""
    from apollo.persistence.models import Concept, DedupDecision, KGEntity

    aapi, _space_id, concept_id, (e1_id, _e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-history"
    )
    fake_neo = _FakeNeo(with_history={concept_id})  # RESOLVES_TO exists -> spare
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: fake_neo)

    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp["removed_concepts"] == 0

    assert await db_session.get(Concept, concept_id) is not None
    assert await db_session.get(KGEntity, e1_id) is not None
    remaining_dedup = (
        await db_session.execute(
            select(func.count())
            .select_from(DedupDecision)
            .where(DedupDecision.concept_id == concept_id)
        )
    ).scalar_one()
    assert remaining_dedup == 1
    # The guard spares it: NO DETACH DELETE was issued.
    assert not any("DETACH DELETE" in cypher for cypher, _p in fake_neo.calls)


_STUDENT_UUID = "00000000-0000-0000-0000-000000000009"


async def _assert_concept_spared(db, aapi, monkeypatch, *, concept_id, set_id):
    """Delete the set with a no-history fake Neo4j and assert the concept survived,
    nothing was reported torn down, and no :Canon DETACH DELETE was issued."""
    from apollo.persistence.models import Concept

    fake_neo = _FakeNeo()
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: fake_neo)
    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db)
    assert resp["deleted"] is True
    assert resp["removed_concepts"] == 0
    assert await db.get(Concept, concept_id) is not None
    assert not any("DETACH DELETE" in cypher for cypher, _p in fake_neo.calls)


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_session(db_session, monkeypatch):
    """A concept any student ever opened has an apollo_sessions row — the only
    ON DELETE RESTRICT FK into apollo_concepts. It must be spared (else the whole
    delete 500s and the set becomes permanently undeletable)."""
    from apollo.persistence.models import ApolloSession

    aapi, space_id, concept_id, _entities, set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-session"
    )
    db_session.add(
        ApolloSession(user_id=_STUDENT_UUID, search_space_id=space_id, concept_id=concept_id)
    )
    await db_session.flush()
    await _assert_concept_spared(
        db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id
    )


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_learner_state(db_session, monkeypatch):
    """apollo_learner_state (the durable per-learner belief) keyed on the concept's
    entities CASCADEs from apollo_kg_entities — the concept must be spared."""
    from apollo.persistence.models import LearnerState

    aapi, space_id, concept_id, (e1_id, _e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-lstate"
    )
    db_session.add(
        LearnerState(
            user_id=_STUDENT_UUID,
            search_space_id=space_id,
            entity_id=e1_id,
            belief=[0.33, 0.33, 0.34],
            mastery=0.5,
            confidence=0.5,
        )
    )
    await db_session.flush()
    await _assert_concept_spared(
        db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id
    )


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_mastery_event(db_session, monkeypatch):
    """apollo_mastery_events (the append-only grading / refit corpus) keyed on the
    concept's entities CASCADEs — and the all-missing grading path writes one with
    NO RESOLVES_TO edge, so the Neo4j guard alone would miss it. Must be spared."""
    from apollo.persistence.models import MasteryEvent

    aapi, space_id, concept_id, (e1_id, _e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-mevent"
    )
    db_session.add(
        MasteryEvent(
            user_id=_STUDENT_UUID,
            search_space_id=space_id,
            entity_id=e1_id,
            event_kind="missing",
            prior_belief=[0.33, 0.33, 0.34],
            posterior_belief=[0.3, 0.3, 0.4],
            mastery_after=0.4,
        )
    )
    await db_session.flush()
    await _assert_concept_spared(
        db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id
    )


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_misconceptions(db_session, monkeypatch):
    """A seed-authored apollo_misconceptions bank marks a shared/seed concept (it
    would CASCADE-delete). Must be spared."""
    from apollo.persistence.models import Misconception

    aapi, _space_id, concept_id, _entities, set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-misc"
    )
    db_session.add(
        Misconception(
            concept_id=concept_id,
            code="mc.speed_pressure",
            description="thinks faster=higher P",
            probe_question="what happens to pressure?",
        )
    )
    await db_session.flush()
    await _assert_concept_spared(
        db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id
    )


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_depended_on_by_another(db_session, monkeypatch):
    """A concept another concept DEPENDS ON (inbound cross-concept prereq: another
    concept's entity -> this concept's entity) is a prerequisite the curriculum
    still needs — deleting it would corrupt the surviving concept's prereq chain."""
    from apollo.persistence.models import Concept, EntityPrereq, KGEntity

    aapi, space_id, concept_id, (e1_id, _e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-depended"
    )
    # A DIFFERENT concept whose entity depends on this concept's entity e1.
    subj_id = (
        await db_session.execute(select(Concept.subject_id).where(Concept.id == concept_id))
    ).scalar_one()
    other = Concept(
        subject_id=subj_id,
        slug="concept-dependent",
        display_name="Dependent",
        canonical_symbols={},
        normalization_map={},
    )
    db_session.add(other)
    await db_session.flush()
    other_entity = KGEntity(
        concept_id=other.id,
        canonical_key="eq.dep",
        kind="equation",
        display_name="dep",
        payload={},
        aliases=[],
    )
    db_session.add(other_entity)
    await db_session.flush()
    db_session.add(EntityPrereq(from_entity_id=other_entity.id, to_entity_id=e1_id))
    await db_session.flush()
    await _assert_concept_spared(
        db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id
    )


@pytest.mark.asyncio
async def test_delete_authored_set_tears_down_mutually_linked_orphans(db_session, monkeypatch):
    """The validation guarantee: when a set's OWN concepts are cross-linked (a
    cross-concept prereq between two concepts the SAME delete orphans — the exact
    corrupted shape the 2026-06-30 audit found in set 4), they must NOT mutually
    protect each other. Both are torn down together."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import (
        AuthoredSet,
        Concept,
        ConceptProblem,
        EntityPrereq,
        KGEntity,
        Subject,
    )
    from database.models import SearchSpace

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)

    space = SearchSpace(name="Course link", slug="aas-link", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subject = Subject(slug="subject-link", display_name="Physics", search_space_id=space.id)
    db_session.add(subject)
    await db_session.flush()
    ca = Concept(
        subject_id=subject.id,
        slug="concept-a",
        display_name="A",
        canonical_symbols={},
        normalization_map={},
    )
    cb = Concept(
        subject_id=subject.id,
        slug="concept-b",
        display_name="B",
        canonical_symbols={},
        normalization_map={},
    )
    db_session.add_all([ca, cb])
    await db_session.flush()
    ea = KGEntity(
        concept_id=ca.id,
        canonical_key="eq.a",
        kind="equation",
        display_name="a",
        payload={},
        aliases=[],
    )
    eb = KGEntity(
        concept_id=cb.id,
        canonical_key="eq.b",
        kind="equation",
        display_name="b",
        payload={},
        aliases=[],
    )
    db_session.add_all([ea, eb])
    await db_session.flush()
    # Cross-concept prereq BETWEEN the two set-owned concepts (ca depends on cb).
    db_session.add(EntityPrereq(from_entity_id=ea.id, to_entity_id=eb.id))
    cpa = ConceptProblem(
        concept_id=ca.id,
        problem_code="scrape.a",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space.id,
    )
    cpb = ConceptProblem(
        concept_id=cb.id,
        problem_code="scrape.b",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space.id,
    )
    db_session.add_all([cpa, cpb])
    await db_session.flush()
    target = AuthoredSet(
        search_space_id=space.id,
        set_index=1,
        status="done",
        result_summary={
            "problems": [
                {"concept_problem_id": cpa.id, "outcome": "promoted"},
                {"concept_problem_id": cpb.id, "outcome": "promoted"},
            ]
        },
    )
    db_session.add(target)
    await db_session.flush()
    ca_id, cb_id, set_id = int(ca.id), int(cb.id), int(target.id)

    fake_neo = _FakeNeo()
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: fake_neo)
    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp["removed_concepts"] == 2
    assert await db_session.get(Concept, ca_id) is None
    assert await db_session.get(Concept, cb_id) is None


@pytest.mark.asyncio
async def test_delete_authored_set_spares_prereq_of_protected_sibling(db_session, monkeypatch):
    """A concept D that is spared by a signal (a session) is a SURVIVOR: if D depends
    on a fellow-orphan C (D's entity -> C's entity), C must ALSO be spared so D's
    prereq chain stays intact. The inbound-prereq check must treat a signal-spared
    in-batch concept as a survivor, not as part of the teardown."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import (
        ApolloSession,
        AuthoredSet,
        Concept,
        ConceptProblem,
        EntityPrereq,
        KGEntity,
        Subject,
    )
    from database.models import SearchSpace

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)

    space = SearchSpace(name="Course prereqspare", slug="aas-prqspare", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subject = Subject(slug="subject-prqspare", display_name="Physics", search_space_id=space.id)
    db_session.add(subject)
    await db_session.flush()
    cc = Concept(
        subject_id=subject.id,
        slug="concept-c",
        display_name="C",
        canonical_symbols={},
        normalization_map={},
    )
    cd = Concept(
        subject_id=subject.id,
        slug="concept-d",
        display_name="D",
        canonical_symbols={},
        normalization_map={},
    )
    db_session.add_all([cc, cd])
    await db_session.flush()
    ec = KGEntity(
        concept_id=cc.id,
        canonical_key="eq.c",
        kind="equation",
        display_name="c",
        payload={},
        aliases=[],
    )
    ed = KGEntity(
        concept_id=cd.id,
        canonical_key="eq.d",
        kind="equation",
        display_name="d",
        payload={},
        aliases=[],
    )
    db_session.add_all([ec, ed])
    await db_session.flush()
    # D depends on C (from=D's entity, to=C's entity).
    db_session.add(EntityPrereq(from_entity_id=ed.id, to_entity_id=ec.id))
    # D is spared by a student session; C has no signal of its own.
    db_session.add(ApolloSession(user_id=_STUDENT_UUID, search_space_id=space.id, concept_id=cd.id))
    cpc = ConceptProblem(
        concept_id=cc.id,
        problem_code="scrape.c",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space.id,
    )
    cpd = ConceptProblem(
        concept_id=cd.id,
        problem_code="scrape.d",
        difficulty="m",
        payload={},
        tier=2,
        search_space_id=space.id,
    )
    db_session.add_all([cpc, cpd])
    await db_session.flush()
    target = AuthoredSet(
        search_space_id=space.id,
        set_index=1,
        status="done",
        result_summary={
            "problems": [
                {"concept_problem_id": cpc.id, "outcome": "promoted"},
                {"concept_problem_id": cpd.id, "outcome": "promoted"},
            ]
        },
    )
    db_session.add(target)
    await db_session.flush()
    cc_id, cd_id, set_id = int(cc.id), int(cd.id), int(target.id)

    fake_neo = _FakeNeo()
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: fake_neo)
    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp["removed_concepts"] == 0  # C spared because survivor D depends on it
    assert await db_session.get(Concept, cc_id) is not None
    assert await db_session.get(Concept, cd_id) is not None


# ---------------------------------------------------------------------------
# H1: every authored-sets endpoint is TEACHER-gated (require_course_teacher),
# not merely course-membership-gated. These exercise the real dependency (no
# monkeypatch on require_course_teacher/require_user), seeding an actual
# CourseMembership row so an enrolled *student* is proven to get 403 while a
# *teacher* clears the gate.
# ---------------------------------------------------------------------------


async def _seed_membership(db, *, user_id: str, search_space_id: int, role: str) -> None:
    from database.models import CourseMembership

    db.add(CourseMembership(user_id=user_id, search_space_id=search_space_id, role=role))
    await db.flush()


def _as_real_user(monkeypatch, aapi, user_id: str) -> None:
    async def _require_user(_request):
        return AuthContext(user_id=user_id, access_token="tok")

    monkeypatch.setattr(aapi, "require_user", _require_user)


_STUDENT_USER = "11111111-1111-1111-1111-111111111111"
_TEACHER_USER = "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_create_authored_set_student_gets_403(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    space_id, _concept_id = await _seed_course(db_session, slug="h1-create")
    await _seed_membership(
        db_session, user_id=_STUDENT_USER, search_space_id=space_id, role="student"
    )
    _as_real_user(monkeypatch, aapi, _STUDENT_USER)

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.create_authored_set(
            request=_FakeRequest(),
            background=_BG(),
            problem=_FakeUpload(b"%PDF p", "problems.pdf"),
            solution=_FakeUpload(b"%PDF s", "solutions.pdf"),
            search_space_id=space_id,
            db=db_session,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_authored_set_teacher_passes(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    space_id, _concept_id = await _seed_course(db_session, slug="h1-create-ok")
    await _seed_membership(
        db_session, user_id=_TEACHER_USER, search_space_id=space_id, role="teacher"
    )
    _as_real_user(monkeypatch, aapi, _TEACHER_USER)
    bg = _BG()

    resp = await aapi.create_authored_set(
        request=_FakeRequest(),
        background=bg,
        problem=_FakeUpload(b"%PDF p", "problems.pdf"),
        solution=_FakeUpload(b"%PDF s", "solutions.pdf"),
        search_space_id=space_id,
        db=db_session,
    )
    assert resp["status"] == "pending"


@pytest.mark.asyncio
async def test_list_authored_sets_student_gets_403(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    space_id, _concept_id = await _seed_course(db_session, slug="h1-list")
    await _seed_membership(
        db_session, user_id=_STUDENT_USER, search_space_id=space_id, role="student"
    )
    _as_real_user(monkeypatch, aapi, _STUDENT_USER)

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.list_authored_sets(
            request=_FakeRequest(), search_space_id=space_id, db=db_session
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_authored_sets_teacher_passes(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    space_id, _concept_id = await _seed_course(db_session, slug="h1-list-ok")
    await _seed_membership(
        db_session, user_id=_TEACHER_USER, search_space_id=space_id, role="teacher"
    )
    _as_real_user(monkeypatch, aapi, _TEACHER_USER)

    resp = await aapi.list_authored_sets(
        request=_FakeRequest(), search_space_id=space_id, db=db_session
    )
    assert resp == {"sets": []}


@pytest.mark.asyncio
async def test_get_authored_set_student_gets_403(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _concept_id = await _seed_course(db_session, slug="h1-get")
    await _seed_membership(
        db_session, user_id=_STUDENT_USER, search_space_id=space_id, role="student"
    )
    _as_real_user(monkeypatch, aapi, _STUDENT_USER)
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.get_authored_set(set_id=int(row.id), request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_authored_set_teacher_passes(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _concept_id = await _seed_course(db_session, slug="h1-get-ok")
    await _seed_membership(
        db_session, user_id=_TEACHER_USER, search_space_id=space_id, role="teacher"
    )
    _as_real_user(monkeypatch, aapi, _TEACHER_USER)
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()

    resp = await aapi.get_authored_set(set_id=int(row.id), request=_FakeRequest(), db=db_session)
    assert resp["set_id"] == int(row.id)


@pytest.mark.asyncio
async def test_delete_authored_set_student_gets_403(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _concept_id = await _seed_course(db_session, slug="h1-delete")
    await _seed_membership(
        db_session, user_id=_STUDENT_USER, search_space_id=space_id, role="student"
    )
    _as_real_user(monkeypatch, aapi, _STUDENT_USER)
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 403
    assert await db_session.get(AuthoredSet, set_id) is not None


@pytest.mark.asyncio
async def test_delete_authored_set_teacher_passes(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _concept_id = await _seed_course(db_session, slug="h1-delete-ok")
    await _seed_membership(
        db_session, user_id=_TEACHER_USER, search_space_id=space_id, role="teacher"
    )
    _as_real_user(monkeypatch, aapi, _TEACHER_USER)
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp["deleted"] is True
    assert await db_session.get(AuthoredSet, set_id) is None


@pytest.mark.asyncio
async def test_approve_held_problem_student_gets_403(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _concept_id = await _seed_course(db_session, slug="h1-approve")
    await _seed_membership(
        db_session, user_id=_STUDENT_USER, search_space_id=space_id, role="student"
    )
    _as_real_user(monkeypatch, aapi, _STUDENT_USER)
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(row.id),
            problem_id=1,
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_approve_held_problem_teacher_clears_gate(db_session, monkeypatch):
    """A teacher clears the auth gate; the request still 404s further down since
    no problem_id=1 exists here -- what matters is that the failure is NOT 403."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _concept_id = await _seed_course(db_session, slug="h1-approve-ok")
    await _seed_membership(
        db_session, user_id=_TEACHER_USER, search_space_id=space_id, role="teacher"
    )
    _as_real_user(monkeypatch, aapi, _TEACHER_USER)
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="done")
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(row.id),
            problem_id=999999,
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- #
# Reversed provisioning: approve-path savepoint + matched-concept threading.
# --------------------------------------------------------------------------- #


def _held_draft_payload() -> dict:
    return {
        "solution_source": "extracted",
        "reference_solution": [
            {
                "step": 1,
                "id": "governing_relation",
                "entry_type": "equation",
                "content": {"symbolic": "M - w*L**2/8", "label": "Governing relation"},
                "depends_on": [],
            }
        ],
        "grounding": [],
        "provenance": {},
    }


async def _seed_held_problem(db, *, slug: str, review_extra: dict | None = None):
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    space, concept = await _seed_course(db, slug=slug)
    prob = ConceptProblem(
        concept_id=concept,
        problem_code=f"held-{slug}",
        difficulty="intro",
        tier=1,
        payload={
            "problem_text": "A beam length L, load w. Find max moment M.",
            "given_values": {"L": 2.0, "w": 3.0},
            "target_unknown": "M",
            "difficulty": "intro",
        },
        search_space_id=space,
        provenance={
            "chunk_content_hash": f"held-{slug}",
            "authored_review": {
                "required": True,
                "reason": "ocr_divergence",
                "ocr_draft": _held_draft_payload(),
                "generated_alt": None,
                **(review_extra or {}),
            },
        },
    )
    db.add(prob)
    await db.flush()
    aset = AuthoredSet(
        search_space_id=space,
        set_index=1,
        status="done",
        result_summary={
            "problems": [{"concept_problem_id": prob.id, "outcome": "held_for_review"}]
        },
    )
    db.add(aset)
    await db.flush()
    return space, concept, prob, aset


@pytest.mark.asyncio
async def test_approve_no_matching_concept_hold_409s(db_session, monkeypatch):
    from fastapi import HTTPException

    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    _space, _concept, prob, aset = await _seed_held_problem(
        db_session, slug="approve-nomatch", review_extra={"reason": "no_matching_concept"}
    )
    with pytest.raises(HTTPException) as exc:
        await aapi.approve_held_problem(
            set_id=int(aset.id),
            problem_id=int(prob.id),
            body=aapi.ApproveBody(reference="ocr"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 409
    assert "no registered concept" in exc.value.detail


@pytest.mark.asyncio
async def test_approve_threads_stored_concept_match(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.provisioning.promote import PromoteResult

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space, concept, prob, aset = await _seed_held_problem(
        db_session,
        slug="approve-thread",
        review_extra={
            "concept_match": {
                "concept_id": None,  # placeholder, patched below
                "slug": None,
            }
        },
    )
    # point the stored match at the seeded concept
    prov = dict(prob.provenance)
    prov["authored_review"]["concept_match"] = {
        "concept_id": concept,
        "slug": "concept-approve-thread",
    }
    prob.provenance = prov
    await db_session.flush()

    captured: dict = {}

    async def _tag_and_mint(db, pair, *, chat_fn, embed_fn, resolved_concept=None):
        captured["resolved_concept"] = resolved_concept
        from apollo.provisioning.tag_mint import MintPlan

        return MintPlan(
            concept_id=resolved_concept.concept_id,
            concept_slug=resolved_concept.slug,
            authored_symbols=[],
            minted_entity_ids={},
            merged_entity_keys=[],
            prereq_pairs=[],
            misconception_keys=[],
        )

    async def _promote(db, neo, **kwargs):
        return PromoteResult(promoted=True)

    monkeypatch.setattr(aapi, "tag_and_mint", _tag_and_mint)
    monkeypatch.setattr(aapi, "promote", _promote)
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: None)
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_k: object())

    out = await aapi.approve_held_problem(
        set_id=int(aset.id),
        problem_id=int(prob.id),
        body=aapi.ApproveBody(reference="ocr"),
        request=_FakeRequest(),
        db=db_session,
    )
    assert out["promoted"] is True
    assert captured["resolved_concept"].concept_id == concept


@pytest.mark.asyncio
async def test_approve_gate_rejection_rolls_back_mint(db_session, monkeypatch):
    """The approve path shares the orchestrator's transactional-mint contract:
    a lint rejection rolls back every KG row the REAL tag_and_mint flushed."""
    from sqlalchemy import func, select

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import KGEntity
    from apollo.provisioning.promote import PromoteResult

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space, concept, prob, aset = await _seed_held_problem(db_session, slug="approve-gate8")
    prov = dict(prob.provenance)
    prov["authored_review"]["concept_match"] = {
        "concept_id": concept,
        "slug": "concept-approve-gate8",
    }
    prob.provenance = prov
    await db_session.flush()

    async def _promote(db, neo, **kwargs):
        return PromoteResult(promoted=False, failed_gate=8, diagnostic="gate 8: duplicate")

    monkeypatch.setattr(aapi, "promote", _promote)
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: None)
    monkeypatch.setattr(aapi, "embed_text", lambda _t: [0.0])
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_k: object())

    out = await aapi.approve_held_problem(
        set_id=int(aset.id),
        problem_id=int(prob.id),
        body=aapi.ApproveBody(reference="ocr"),
        request=_FakeRequest(),
        db=db_session,
    )
    assert out["promoted"] is False and out["failed_gate"] == 8
    n = (
        await db_session.execute(
            select(func.count()).select_from(KGEntity).where(KGEntity.concept_id == concept)
        )
    ).scalar_one()
    assert n == 0  # the real mint's entity was rolled back with the savepoint
    # the hold is preserved (still requires review)
    fresh = await db_session.get(type(prob), int(prob.id))
    assert fresh.provenance["authored_review"]["required"] is True


@pytest.mark.asyncio
async def test_approve_tag_mint_error_reports_without_committing(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.provisioning.tag_mint import TagMintError

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    _space, _concept, prob, aset = await _seed_held_problem(db_session, slug="approve-tme")

    async def _raise_tme(*_a, **_k):
        raise TagMintError("opposes an unknown entity key")

    monkeypatch.setattr(aapi, "tag_and_mint", _raise_tme)
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: None)
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_k: object())

    out = await aapi.approve_held_problem(
        set_id=int(aset.id),
        problem_id=int(prob.id),
        body=aapi.ApproveBody(reference="ocr"),
        request=_FakeRequest(),
        db=db_session,
    )
    assert out["promoted"] is False
    assert out["diagnostic"].startswith("tag_mint_error")
    fresh = await db_session.get(type(prob), int(prob.id))
    assert fresh.provenance["authored_review"]["required"] is True  # hold preserved


# --------------------------------------------------------------------------- #
# Per-set GET review enrichment (teacher review UI surface)
# --------------------------------------------------------------------------- #


async def _seed_enrichment_set(db, *, slug: str, payload: dict, provenance: dict):
    """One held problem + its authored set, returning ``(space, prob, aset)``."""
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    space, concept = await _seed_course(db, slug=slug)
    prob = ConceptProblem(
        concept_id=concept,
        problem_code=f"enrich-{slug}",
        difficulty="intro",
        tier=1,
        payload=payload,
        search_space_id=space,
        provenance=provenance,
    )
    db.add(prob)
    await db.flush()
    aset = AuthoredSet(
        search_space_id=space,
        set_index=1,
        status="done",
        result_summary={
            "problems": [
                {
                    "concept_problem_id": int(prob.id),
                    "outcome": "held_for_review",
                    "reason": provenance.get("authored_review", {}).get("reason"),
                }
            ],
            "counts": {"held_for_review": 1},
        },
    )
    db.add(aset)
    await db.flush()
    return space, prob, aset


@pytest.mark.asyncio
async def test_get_authored_set_enriches_held_problem(db_session, monkeypatch):
    """A held problem's GET entry carries the question text and the WHITELISTED
    review projection — trimmed drafts only (no grounding spans, no concept_match
    or any other provenance key leaking into the response)."""
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    steps = [{"step": 1, "entry_type": "equation", "id": "eq1", "content": {"latex": "F=ma"}}]
    _space, _prob, aset = await _seed_enrichment_set(
        db_session,
        slug="enrich-held",
        payload={"problem_text": "What force accelerates the cart?"},
        provenance={
            "authored_review": {
                "required": True,
                "reason": "generated_no_match",
                "ocr_confidence": 0.42,
                "match_method": "label",
                "ocr_draft": {
                    "solution_source": "generated",
                    "reference_solution": steps,
                    "grounding": [{"chunk_id": 9, "text": "secret solution chunk"}],
                    "provenance": {"chunk_content_hash": "abc"},
                },
                "generated_alt": None,
                "concept_match": {"concept_id": 5, "slug": "forces", "rationale": "internal"},
            }
        },
    )

    detail = await aapi.get_authored_set(set_id=int(aset.id), request=_FakeRequest(), db=db_session)

    (problem,) = detail["result_summary"]["problems"]
    assert problem["problem_text"] == "What force accelerates the cart?"
    assert problem["problem_text_truncated"] is False
    review = problem["review"]
    assert set(review) == {"required", "reason", "approved_reference", "ocr_draft", "generated_alt"}
    assert review["required"] is True
    assert review["reason"] == "generated_no_match"
    assert review["generated_alt"] is None
    draft = review["ocr_draft"]
    assert set(draft) == {"solution_source", "reference_solution"}
    assert draft["solution_source"] == "generated"
    assert draft["reference_solution"] == steps
    # The frozen counts stay verbatim; the UI recomputes from live review state.
    assert detail["result_summary"]["counts"] == {"held_for_review": 1}


@pytest.mark.asyncio
async def test_get_authored_set_review_after_approval_omits_drafts(db_session, monkeypatch):
    """Once approved (``required`` flipped false), the review projection exposes
    the CURRENT state + which reference was approved, but no draft bodies."""
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    _space, _prob, aset = await _seed_enrichment_set(
        db_session,
        slug="enrich-approved",
        payload={"problem_text": "Q"},
        provenance={
            "authored_review": {
                "required": False,
                "reason": "generated_no_match",
                "approved_reference": "ocr",
                "ocr_draft": {"solution_source": "generated", "reference_solution": []},
            }
        },
    )

    detail = await aapi.get_authored_set(set_id=int(aset.id), request=_FakeRequest(), db=db_session)

    review = detail["result_summary"]["problems"][0]["review"]
    assert review == {
        "required": False,
        "reason": "generated_no_match",
        "approved_reference": "ocr",
    }


@pytest.mark.asyncio
async def test_get_authored_set_enriches_no_match_hold(db_session, monkeypatch):
    """A ``no_matching_concept`` hold stores no draft: the projection still carries
    the reason + question text (so the UI can render the concept-gap card) with
    both draft slots null — and nothing from ``concept_match`` leaks."""
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    _space, _prob, aset = await _seed_enrichment_set(
        db_session,
        slug="enrich-nomatch",
        payload={"problem_text": "Explain the unmatched idea."},
        provenance={
            "authored_review": {
                "required": True,
                "reason": "no_matching_concept",
                "concept_match": {"no_match": True, "rationale": "nothing registered fits"},
            }
        },
    )

    detail = await aapi.get_authored_set(set_id=int(aset.id), request=_FakeRequest(), db=db_session)

    problem = detail["result_summary"]["problems"][0]
    assert problem["problem_text"] == "Explain the unmatched idea."
    review = problem["review"]
    assert review["required"] is True
    assert review["reason"] == "no_matching_concept"
    assert review["ocr_draft"] is None
    assert review["generated_alt"] is None
    assert "concept_match" not in review


@pytest.mark.asyncio
async def test_get_authored_set_caps_problem_text(db_session, monkeypatch):
    """``problem_text`` obeys the ``_LIST_OCR_TEXT_CAP`` size discipline."""
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    long_text = "x" * (aapi._LIST_OCR_TEXT_CAP + 50)
    _space, _prob, aset = await _seed_enrichment_set(
        db_session,
        slug="enrich-cap",
        payload={"problem_text": long_text},
        provenance={"authored_review": {"required": True, "reason": "generated_no_match"}},
    )

    detail = await aapi.get_authored_set(set_id=int(aset.id), request=_FakeRequest(), db=db_session)

    problem = detail["result_summary"]["problems"][0]
    assert problem["problem_text_truncated"] is True
    assert len(problem["problem_text"]) == aapi._LIST_OCR_TEXT_CAP


@pytest.mark.asyncio
async def test_get_authored_set_enrichment_null_safety(db_session, monkeypatch):
    """Entries with no ``concept_problem_id``, a deleted row, a malformed entry, or
    a row with no ``authored_review`` provenance pass through without enrichment
    errors (old-shape sets keep working)."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet, ConceptProblem

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space, concept = await _seed_course(db_session, slug="enrich-null")
    prob = ConceptProblem(
        concept_id=concept,
        problem_code="enrich-null",
        difficulty="intro",
        tier=1,
        payload={},
        search_space_id=space,
        provenance={},
    )
    db_session.add(prob)
    await db_session.flush()
    aset = AuthoredSet(
        search_space_id=space,
        set_index=1,
        status="done",
        result_summary={
            "problems": [
                {"concept_problem_id": None, "outcome": "rejected", "diagnostic": "gate"},
                {"concept_problem_id": 987654, "outcome": "promoted"},
                "malformed-entry",
                {"concept_problem_id": int(prob.id), "outcome": "promoted"},
            ]
        },
    )
    db_session.add(aset)
    await db_session.flush()

    detail = await aapi.get_authored_set(set_id=int(aset.id), request=_FakeRequest(), db=db_session)

    problems = detail["result_summary"]["problems"]
    assert problems[0] == {"concept_problem_id": None, "outcome": "rejected", "diagnostic": "gate"}
    assert problems[1] == {"concept_problem_id": 987654, "outcome": "promoted"}
    assert problems[2] == "malformed-entry"
    # The live row exists but has no authored_review: text enriches, review null.
    assert problems[3]["problem_text"] == ""
    assert problems[3]["review"] is None


@pytest.mark.asyncio
async def test_get_authored_set_no_ids_skips_lookup(db_session, monkeypatch):
    """A problems list carrying no concept_problem_ids never queries ConceptProblem."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_require_member)
    space, _concept = await _seed_course(db_session, slug="enrich-noids")
    aset = AuthoredSet(
        search_space_id=space,
        set_index=1,
        status="done",
        result_summary={"problems": [{"concept_problem_id": None, "outcome": "rejected"}]},
    )
    db_session.add(aset)
    await db_session.flush()

    detail = await aapi.get_authored_set(set_id=int(aset.id), request=_FakeRequest(), db=db_session)

    assert detail["result_summary"]["problems"] == [
        {"concept_problem_id": None, "outcome": "rejected"}
    ]
