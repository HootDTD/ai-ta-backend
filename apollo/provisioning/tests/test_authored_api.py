from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)
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


def test_get_neo4j_client_delegates(monkeypatch):
    import apollo.api as apollo_api
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(apollo_api, "get_neo4j_client", lambda: "neo-client")
    assert aapi.get_neo4j_client() == "neo-client"


@pytest.mark.asyncio
async def test_background_runner_returns_when_row_vanishes(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, **_kwargs):
        return 101 if role == "problem" else 102

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    # No AuthoredSet row for this id -> the post-index fetch is None -> early return.
    await aapi._run_set_background(
        set_id=999999,
        search_space_id=1,
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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)
    aset = AuthoredSet(search_space_id=space, set_index=1, status="done")
    db_session.add(aset)
    await db_session.flush()
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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)
    aset = AuthoredSet(search_space_id=space, set_index=1, status="done")
    db_session.add(aset)
    await db_session.flush()
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
async def test_list_and_detail_are_course_gated(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    search_space_id, _concept_id = await _seed_course(db_session, slug="list")
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    seen = []

    async def _member(**kwargs):
        seen.append(kwargs["search_space_id"])

    monkeypatch.setattr(aapi, "require_course_member", _member)
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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)
    aset = AuthoredSet(search_space_id=search_space_id, set_index=1, status="done")
    db_session.add(aset)
    await db_session.flush()
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
