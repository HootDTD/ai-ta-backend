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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)

    space_id, concept_id = await _seed_course(db_session, slug="aas-del")

    # Two reference docs for the target set, each with a chunk.
    pdoc = AITADocument(title="p", content="c", content_hash="ph", unique_identifier_hash="pu",
                        search_space_id=space_id, status={"state": "apollo_reference"})
    sdoc = AITADocument(title="s", content="c", content_hash="sh", unique_identifier_hash="su",
                        search_space_id=space_id, status={"state": "apollo_reference"})
    db_session.add_all([pdoc, sdoc])
    await db_session.flush()
    db_session.add_all([
        AITAChunk(document_id=pdoc.id, content="pc"),
        AITAChunk(document_id=sdoc.id, content="sc"),
    ])

    # Two ConceptProblems minted by the target set, one by a sibling set.
    cp1 = ConceptProblem(concept_id=concept_id, problem_code="scrape.a", difficulty="m",
                         payload={}, tier=2, search_space_id=space_id)
    cp2 = ConceptProblem(concept_id=concept_id, problem_code="scrape.b", difficulty="m",
                         payload={}, tier=2, search_space_id=space_id)
    sibling = ConceptProblem(concept_id=concept_id, problem_code="scrape.z", difficulty="m",
                             payload={}, tier=2, search_space_id=space_id)
    db_session.add_all([cp1, cp2, sibling])
    await db_session.flush()

    target = AuthoredSet(
        search_space_id=space_id, set_index=1, status="done",
        problem_document_id=pdoc.id, solution_document_id=sdoc.id,
        result_summary={"problems": [
            {"concept_problem_id": cp1.id, "outcome": "promoted"},
            {"concept_problem_id": cp2.id, "outcome": "held_for_review"},
            {"concept_problem_id": None, "outcome": "rejected"},
        ]},
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
    remaining_chunks = (await db_session.execute(
        select(func.count()).select_from(AITAChunk).where(AITAChunk.document_id.in_([pdoc_id, sdoc_id]))
    )).scalar_one()
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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)
    space_id, _ = await _seed_course(db_session, slug="aas-del-failed")

    row = AuthoredSet(search_space_id=space_id, set_index=1, status="failed",
                      result_summary={"error": "boom"})
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    resp = await aapi.delete_authored_set(set_id=set_id, request=_FakeRequest(), db=db_session)
    assert resp == {"deleted": True, "removed_problems": 0, "removed_documents": 0,
                    "removed_concepts": 0}
    assert await db_session.get(AuthoredSet, set_id) is None


@pytest.mark.asyncio
async def test_delete_authored_set_404(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    with pytest.raises(aapi.HTTPException) as exc:
        await aapi.delete_authored_set(set_id=999999, request=_FakeRequest(), db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_authored_set_enforces_membership(db_session, monkeypatch):
    """A non-member is rejected and the set survives."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    async def _deny_member(**_kwargs):
        raise aapi.HTTPException(status_code=403, detail="not a member")

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_member", _deny_member)
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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)
    space_id, concept_id = await _seed_course(db, slug=slug)

    e1 = KGEntity(concept_id=concept_id, canonical_key="eq.a", kind="equation",
                  display_name="a", payload={}, aliases=[])
    e2 = KGEntity(concept_id=concept_id, canonical_key="proc.b", kind="procedure",
                  display_name="b", payload={}, aliases=[])
    db.add_all([e1, e2])
    await db.flush()
    db.add(EntityPrereq(from_entity_id=e2.id, to_entity_id=e1.id))
    db.add(DedupDecision(search_space_id=space_id, concept_id=concept_id,
                         candidate_key="eq.a", method="slug", similarity=None, verdict="distinct"))
    cp = ConceptProblem(concept_id=concept_id, problem_code="scrape.a", difficulty="m",
                        payload={}, tier=2, search_space_id=space_id)
    db.add(cp)
    await db.flush()
    entity_ids = (int(e1.id), int(e2.id))

    target = AuthoredSet(
        search_space_id=space_id, set_index=1, status="done",
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
    remaining_prereqs = (await db_session.execute(
        select(func.count()).select_from(EntityPrereq).where(EntityPrereq.from_entity_id == e2_id)
    )).scalar_one()
    assert remaining_prereqs == 0
    remaining_dedup = (await db_session.execute(
        select(func.count()).select_from(DedupDecision).where(DedupDecision.concept_id == concept_id)
    )).scalar_one()
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
    remaining_dedup = (await db_session.execute(
        select(func.count()).select_from(DedupDecision).where(DedupDecision.concept_id == concept_id)
    )).scalar_one()
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
    db_session.add(ApolloSession(user_id=_STUDENT_UUID, search_space_id=space_id, concept_id=concept_id))
    await db_session.flush()
    await _assert_concept_spared(db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id)


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_learner_state(db_session, monkeypatch):
    """apollo_learner_state (the durable per-learner belief) keyed on the concept's
    entities CASCADEs from apollo_kg_entities — the concept must be spared."""
    from apollo.persistence.models import LearnerState

    aapi, space_id, concept_id, (e1_id, _e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-lstate"
    )
    db_session.add(LearnerState(
        user_id=_STUDENT_UUID, search_space_id=space_id, entity_id=e1_id,
        belief=[0.33, 0.33, 0.34], mastery=0.5, confidence=0.5,
    ))
    await db_session.flush()
    await _assert_concept_spared(db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id)


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_mastery_event(db_session, monkeypatch):
    """apollo_mastery_events (the append-only grading / refit corpus) keyed on the
    concept's entities CASCADEs — and the all-missing grading path writes one with
    NO RESOLVES_TO edge, so the Neo4j guard alone would miss it. Must be spared."""
    from apollo.persistence.models import MasteryEvent

    aapi, space_id, concept_id, (e1_id, _e2_id), set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-mevent"
    )
    db_session.add(MasteryEvent(
        user_id=_STUDENT_UUID, search_space_id=space_id, entity_id=e1_id,
        event_kind="missing", prior_belief=[0.33, 0.33, 0.34],
        posterior_belief=[0.3, 0.3, 0.4], mastery_after=0.4,
    ))
    await db_session.flush()
    await _assert_concept_spared(db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id)


@pytest.mark.asyncio
async def test_delete_authored_set_spares_concept_with_misconceptions(db_session, monkeypatch):
    """A seed-authored apollo_misconceptions bank marks a shared/seed concept (it
    would CASCADE-delete). Must be spared."""
    from apollo.persistence.models import Misconception

    aapi, _space_id, concept_id, _entities, set_id = await _seed_orphanable_concept_kg(
        db_session, monkeypatch, slug="aas-misc"
    )
    db_session.add(Misconception(
        concept_id=concept_id, code="mc.speed_pressure", description="thinks faster=higher P",
        probe_question="what happens to pressure?",
    ))
    await db_session.flush()
    await _assert_concept_spared(db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id)


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
    subj_id = (await db_session.execute(
        select(Concept.subject_id).where(Concept.id == concept_id)
    )).scalar_one()
    other = Concept(subject_id=subj_id, slug="concept-dependent", display_name="Dependent",
                    canonical_symbols={}, normalization_map={})
    db_session.add(other)
    await db_session.flush()
    other_entity = KGEntity(concept_id=other.id, canonical_key="eq.dep", kind="equation",
                            display_name="dep", payload={}, aliases=[])
    db_session.add(other_entity)
    await db_session.flush()
    db_session.add(EntityPrereq(from_entity_id=other_entity.id, to_entity_id=e1_id))
    await db_session.flush()
    await _assert_concept_spared(db_session, aapi, monkeypatch, concept_id=concept_id, set_id=set_id)


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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)

    space = SearchSpace(name="Course link", slug="aas-link", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subject = Subject(slug="subject-link", display_name="Physics", search_space_id=space.id)
    db_session.add(subject)
    await db_session.flush()
    ca = Concept(subject_id=subject.id, slug="concept-a", display_name="A",
                 canonical_symbols={}, normalization_map={})
    cb = Concept(subject_id=subject.id, slug="concept-b", display_name="B",
                 canonical_symbols={}, normalization_map={})
    db_session.add_all([ca, cb])
    await db_session.flush()
    ea = KGEntity(concept_id=ca.id, canonical_key="eq.a", kind="equation",
                  display_name="a", payload={}, aliases=[])
    eb = KGEntity(concept_id=cb.id, canonical_key="eq.b", kind="equation",
                  display_name="b", payload={}, aliases=[])
    db_session.add_all([ea, eb])
    await db_session.flush()
    # Cross-concept prereq BETWEEN the two set-owned concepts (ca depends on cb).
    db_session.add(EntityPrereq(from_entity_id=ea.id, to_entity_id=eb.id))
    cpa = ConceptProblem(concept_id=ca.id, problem_code="scrape.a", difficulty="m",
                         payload={}, tier=2, search_space_id=space.id)
    cpb = ConceptProblem(concept_id=cb.id, problem_code="scrape.b", difficulty="m",
                         payload={}, tier=2, search_space_id=space.id)
    db_session.add_all([cpa, cpb])
    await db_session.flush()
    target = AuthoredSet(
        search_space_id=space.id, set_index=1, status="done",
        result_summary={"problems": [
            {"concept_problem_id": cpa.id, "outcome": "promoted"},
            {"concept_problem_id": cpb.id, "outcome": "promoted"},
        ]},
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
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)

    space = SearchSpace(name="Course prereqspare", slug="aas-prqspare", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subject = Subject(slug="subject-prqspare", display_name="Physics", search_space_id=space.id)
    db_session.add(subject)
    await db_session.flush()
    cc = Concept(subject_id=subject.id, slug="concept-c", display_name="C",
                 canonical_symbols={}, normalization_map={})
    cd = Concept(subject_id=subject.id, slug="concept-d", display_name="D",
                 canonical_symbols={}, normalization_map={})
    db_session.add_all([cc, cd])
    await db_session.flush()
    ec = KGEntity(concept_id=cc.id, canonical_key="eq.c", kind="equation",
                  display_name="c", payload={}, aliases=[])
    ed = KGEntity(concept_id=cd.id, canonical_key="eq.d", kind="equation",
                  display_name="d", payload={}, aliases=[])
    db_session.add_all([ec, ed])
    await db_session.flush()
    # D depends on C (from=D's entity, to=C's entity).
    db_session.add(EntityPrereq(from_entity_id=ed.id, to_entity_id=ec.id))
    # D is spared by a student session; C has no signal of its own.
    db_session.add(ApolloSession(user_id=_STUDENT_UUID, search_space_id=space.id, concept_id=cd.id))
    cpc = ConceptProblem(concept_id=cc.id, problem_code="scrape.c", difficulty="m",
                         payload={}, tier=2, search_space_id=space.id)
    cpd = ConceptProblem(concept_id=cd.id, problem_code="scrape.d", difficulty="m",
                         payload={}, tier=2, search_space_id=space.id)
    db_session.add_all([cpc, cpd])
    await db_session.flush()
    target = AuthoredSet(
        search_space_id=space.id, set_index=1, status="done",
        result_summary={"problems": [
            {"concept_problem_id": cpc.id, "outcome": "promoted"},
            {"concept_problem_id": cpd.id, "outcome": "promoted"},
        ]},
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
