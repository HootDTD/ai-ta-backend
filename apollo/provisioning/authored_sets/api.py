"""Teacher-gated HTTP surface for authored problem/solution sets (WU-AAS).

POST indexes both docs hidden from student retrieval, persists the pairing, and
runs provisioning in an in-process background task. GET endpoints poll status
and result summaries; approve promotes a held reference chosen by the teacher.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apollo.auth_deps import require_course_teacher, require_user
from apollo.persistence.models import (
    ApolloSession,
    AuthoredSet,
    Concept,
    ConceptProblem,
    DedupDecision,
    EntityPrereq,
    KGEntity,
    LearnerState,
    MasteryEvent,
    Misconception,
)
from apollo.provisioning.authored_sets.indexing import index_authored_doc
from apollo.provisioning.authored_sets.orchestrator import (
    _authored_concept_dup_hashes,
    _tag_mint_chat_fn,
    run_authored_set_provisioning,
)
from apollo.provisioning.metered_chat import MeteredChat
from apollo.provisioning.promote import promote
from apollo.provisioning.solution import ReferenceSolutionDraft, build_approved_pair
from apollo.provisioning.tag_mint import tag_and_mint
from database.models import AITADocument
from database.session import get_async_session, get_db_session
from indexing.document_embedder import embed_text

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["apollo-authored-sets"])


class ApproveBody(BaseModel):
    reference: Literal["ocr", "generated"] = "ocr"


def get_neo4j_client():
    """Late import avoids a module cycle while keeping a test patch seam."""
    from apollo.api import get_neo4j_client as _get_neo4j_client

    return _get_neo4j_client()


async def _next_set_index(db: AsyncSession, search_space_id: int) -> int:
    cur = (
        await db.execute(
            select(func.coalesce(func.max(AuthoredSet.set_index), 0)).where(
                AuthoredSet.search_space_id == search_space_id
            )
        )
    ).scalar_one()
    return int(cur) + 1


@router.post("/authored-sets")
async def create_authored_set(
    request: Request,
    background: BackgroundTasks,
    problem: UploadFile = File(...),
    solution: UploadFile = File(...),
    search_space_id: int = Form(...),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)

    problem_bytes = await problem.read()
    solution_bytes = await solution.read()
    set_index = await _next_set_index(db, search_space_id)

    row = AuthoredSet(search_space_id=search_space_id, set_index=set_index, status="pending")
    db.add(row)
    await db.flush()
    set_id = int(row.id)
    await db.commit()

    background.add_task(
        _run_set_background,
        set_id=set_id,
        search_space_id=search_space_id,
        set_index=set_index,
        problem_bytes=problem_bytes,
        problem_title=problem.filename or f"Problem Set {set_index}",
        solution_bytes=solution_bytes,
        solution_title=solution.filename or f"Solution Set {set_index}",
    )
    return {"set_id": set_id, "set_index": set_index, "status": "pending"}


async def _run_set_background(
    *,
    set_id: int,
    search_space_id: int,
    set_index: int,
    problem_bytes: bytes,
    problem_title: str,
    solution_bytes: bytes,
    solution_title: str,
) -> None:
    """Own a fresh session; request-scoped sessions are closed before this runs."""
    try:
        async with get_async_session() as db:
            await _set_status(db, set_id, "indexing")
            problem_document_id = await index_authored_doc(
                db,
                search_space_id=search_space_id,
                file_bytes=problem_bytes,
                title=problem_title,
                set_index=set_index,
                role="problem",
            )
            solution_document_id = await index_authored_doc(
                db,
                search_space_id=search_space_id,
                file_bytes=solution_bytes,
                title=solution_title,
                set_index=set_index,
                role="solution",
            )

            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.problem_document_id = problem_document_id
            row.solution_document_id = solution_document_id
            row.status = "provisioning"
            row.updated_at = datetime.now(UTC)
            await db.commit()

            report = await run_authored_set_provisioning(
                db,
                get_neo4j_client(),
                search_space_id=search_space_id,
                problem_document_id=problem_document_id,
                solution_document_id=solution_document_id,
                metered_chat=_make_metered_chat(document_id=problem_document_id),
            )
            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.result_summary = report.model_dump()
            row.status = "done"
            row.updated_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - persist failed status, never escape task
        _LOG.exception("authored_set_background_failed", extra={"set_id": set_id})
        async with get_async_session() as db:
            await _set_status(db, set_id, "failed", diagnostic=str(exc))


def _make_metered_chat(*, document_id: int) -> MeteredChat:
    ingest_run = SimpleNamespace(
        id=None,
        llm_calls=0,
        llm_tokens_in=0,
        llm_tokens_out=0,
        # Decimal, not float: ``cost_usd_for`` returns Decimal and ``record_usage``
        # does ``llm_cost_usd += <Decimal>`` — a float seed raises
        # "unsupported operand type(s) for +=: 'float' and 'decimal.Decimal'"
        # on the first metered LLM call, failing the whole authored-set run.
        llm_cost_usd=Decimal("0"),
    )
    return MeteredChat(ingest_run=ingest_run, document_id=document_id)


async def _set_status(
    db: AsyncSession,
    set_id: int,
    status: str,
    *,
    diagnostic: str | None = None,
) -> None:
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        return
    row.status = status  # type: ignore[assignment]
    row.updated_at = datetime.now(UTC)  # type: ignore[assignment]
    if diagnostic is not None:
        row.result_summary = {**(row.result_summary or {}), "error": diagnostic}  # type: ignore[assignment]
    await db.commit()


@router.get("/authored-sets")
async def list_authored_sets(
    request: Request,
    search_space_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)
    rows = (
        (
            await db.execute(
                select(AuthoredSet)
                .where(AuthoredSet.search_space_id == search_space_id)
                .order_by(AuthoredSet.set_index.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "sets": [
            {
                "set_id": int(row.id),
                "set_index": row.set_index,
                "status": row.status,
                "problem_document_id": row.problem_document_id,
                "solution_document_id": row.solution_document_id,
            }
            for row in rows
        ]
    }


@router.get("/authored-sets/{set_id}")
async def get_authored_set(
    set_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_teacher(db=db, auth=auth, search_space_id=int(row.search_space_id))
    return {
        "set_id": int(row.id),
        "set_index": row.set_index,
        "status": row.status,
        "problem_document_id": row.problem_document_id,
        "solution_document_id": row.solution_document_id,
        "result_summary": row.result_summary or {},
    }


async def _protected_concepts(db: AsyncSession, concept_ids: list[int]) -> set[int]:
    """Of the candidate concepts, return those that must NOT be torn down because
    they carry a Postgres footprint beyond the deleted set. STRICT / conservative:
    ANY single signal spares the whole concept — under-tearing-down is the safe
    direction (it only leaves KG behind; it never destroys data or crashes). Signals:

      * ``apollo_sessions.concept_id`` — a student opened this concept. This is ALSO
        the only ``ON DELETE RESTRICT`` FK into ``apollo_concepts`` (migration 018),
        so sparing session-bound concepts is what keeps ``DELETE apollo_concepts``
        from hard-failing the whole delete.
      * ``apollo_learner_state`` / ``apollo_mastery_events`` keyed on the concept's
        entities — the durable learner belief snapshot + the append-only grading /
        model-refit corpus. Both CASCADE from ``apollo_kg_entities``, so tearing the
        concept down would silently destroy them.
      * ``apollo_misconceptions.concept_id`` — a seed-authored misconception bank
        (CASCADE → silent loss); its presence marks a shared/seed concept.
      * an INBOUND cross-concept prereq edge from a SURVIVING concept — an entity of
        a concept NOT in this teardown batch depends on one of this concept's
        entities, so this concept is a prerequisite the surviving curriculum graph
        still needs; deleting it would corrupt that concept's prereq chain. (A
        dependency from a FELLOW candidate does NOT spare it — both go away
        together, which is what lets a whole corrupted set's concepts be cleared.)
    """
    protected: set[int] = set()

    async def _collect(stmt) -> None:
        protected.update(
            int(c) for c in (await db.execute(stmt)).scalars().all() if c is not None
        )

    await _collect(
        select(ApolloSession.concept_id)
        .where(ApolloSession.concept_id.in_(concept_ids))
        .distinct()
    )
    await _collect(
        select(Misconception.concept_id)
        .where(Misconception.concept_id.in_(concept_ids))
        .distinct()
    )
    await _collect(
        select(KGEntity.concept_id)
        .join(LearnerState, LearnerState.entity_id == KGEntity.id)
        .where(KGEntity.concept_id.in_(concept_ids))
        .distinct()
    )
    await _collect(
        select(KGEntity.concept_id)
        .join(MasteryEvent, MasteryEvent.entity_id == KGEntity.id)
        .where(KGEntity.concept_id.in_(concept_ids))
        .distinct()
    )
    # Inbound cross-concept prereq: FROM a SURVIVING concept's entity TO this
    # concept's entity (``from`` depends on ``to``). "Surviving" excludes the
    # concepts still slated for teardown — i.e. candidates NOT already spared by a
    # signal above (``still_candidate``). Excluding the already-``protected`` set
    # matters: a concept spared by e.g. a session is a survivor whose OWN
    # prerequisites must also be kept, so its dependency must spare its target. A
    # dependency from a fellow STILL-candidate does not spare (both clear together,
    # so a corrupted set's mutually-linked concepts still go). RESIDUAL (accepted as
    # safe KG drift, never data loss): this is a single pass, not a fixpoint, so a
    # concept protected ONLY transitively through this same prereq relation is not
    # re-fed as a survivor — a deep prereq chain among orphans may shed one edge.
    still_candidate = [cid for cid in concept_ids if cid not in protected]
    prereq_target = aliased(KGEntity)
    prereq_source = aliased(KGEntity)
    await _collect(
        select(prereq_target.concept_id)
        .join(EntityPrereq, EntityPrereq.to_entity_id == prereq_target.id)
        .join(prereq_source, prereq_source.id == EntityPrereq.from_entity_id)
        .where(prereq_target.concept_id.in_(still_candidate))
        .where(prereq_source.concept_id.notin_(still_candidate))
        .distinct()
    )
    return protected


async def _concepts_with_canon_history(neo, concept_ids: list[int]) -> set[int]:
    """Return the subset of ``concept_ids`` whose ``:Canon`` nodes carry at least one
    incoming ``RESOLVES_TO`` edge — i.e. real student grading history. Read-only; a
    concept with such history must NOT be torn down. Callers pass a non-empty list
    (an ``UNWIND []`` would be a harmless no-op regardless)."""
    async with neo.session() as s:
        result = await s.run(
            "UNWIND $cids AS cid\n"
            "MATCH (c:Canon {concept_id: cid})<-[:RESOLVES_TO]-()\n"
            "RETURN DISTINCT cid AS cid",
            cids=concept_ids,
        )
        return {int(rec["cid"]) async for rec in result}


async def _detach_delete_canon(neo, concept_ids: list[int]) -> None:
    """DETACH DELETE each concept's ``:Canon`` nodes, GUARDED by the absence of an
    incoming ``RESOLVES_TO`` so student grading history is never destroyed (a
    RESOLVES_TO that appeared after the read below is still spared). Idempotent — no
    shared txn with Postgres, and a re-run over already-gone nodes is a no-op.
    Callers pass a non-empty list (an ``UNWIND []`` would be a harmless no-op)."""
    async with neo.session() as s:
        await s.run(
            "UNWIND $cids AS cid\n"
            "MATCH (c:Canon {concept_id: cid})\n"
            "WHERE NOT (c)<-[:RESOLVES_TO]-()\n"
            "DETACH DELETE c",
            cids=concept_ids,
        )


_IN_FLIGHT_STATUSES = frozenset({"pending", "indexing", "provisioning"})


@router.delete("/authored-sets/{set_id}")
async def delete_authored_set(
    set_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Remove an authored set and everything it produced.

    Deletable in any TERMINAL status (``done`` / ``failed``) — the motivation is
    clearing failed/stuck runs that otherwise pile up on the teacher console
    with no way to remove them. Rejected with 409 while the set is IN-FLIGHT
    (``pending`` / ``indexing`` / ``provisioning``): ``_run_set_background``
    writes Neo4j ``:Canon`` nodes per-candidate outside the Postgres
    transaction, so deleting mid-run would orphan ``:Canon`` nodes this
    endpoint's teardown can never see (it only reads the finished
    ``result_summary``). To recover a dead in-flight run (e.g. the worker
    crashed), mark the set ``failed`` first — the API itself doesn't provide
    that yet (no stuck-run watchdog exists), so today that's a manual status
    update. Cascade for the terminal-state delete that IS allowed:

      * the ConceptProblems this set minted (recorded per-problem in
        ``result_summary``) — deleting the rows is what pulls the content out of
        tutoring (the student selector filters ``tier == 2 AND concept_id``);
      * the two hidden reference documents (chunks cascade via the
        ``aita_chunks`` ON DELETE CASCADE FK);
      * the ``apollo_authored_sets`` row itself.

    Per-concept KG teardown is STRICTLY scoped to concepts this set fully ORPHANED.
    A concept is torn down ONLY if it has ZERO footprint of any kind beyond this
    set: no remaining ``ConceptProblem``s, no Postgres student/seed footprint
    (``_protected_concepts`` — sessions, learner_state, mastery_events,
    misconceptions, or an inbound cross-concept prereq), AND no ``:Canon`` student
    ``RESOLVES_TO`` history. For those (and ONLY those) the reference graph a plain
    delete used to leave behind is torn down: ``apollo_dedup_decisions`` +
    ``apollo_concepts`` (KGEntity + ``apollo_entity_prereqs`` cascade) in Postgres,
    and the guarded ``:Canon`` nodes in Neo4j. Every ambiguous case spares the
    concept — under-tearing-down only leaves KG behind, whereas over-tearing-down
    would 500 (the ``apollo_sessions`` RESTRICT FK) or destroy student data.
    """
    auth = await require_user(request)
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_teacher(db=db, auth=auth, search_space_id=int(row.search_space_id))
    if row.status in _IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"authored set is still {row.status}; mark it failed before deleting",
        )

    problems = (row.result_summary or {}).get("problems") or []  # type: ignore[call-overload]
    problem_ids = sorted(
        {
            int(p["concept_problem_id"])
            for p in problems
            if isinstance(p, dict) and p.get("concept_problem_id") is not None
        }
    )
    removed_problems = 0
    # Capture the concepts these problems belong to BEFORE deleting the rows — once
    # they are gone the concept link is unrecoverable.
    affected_concept_ids: list[int] = []
    if problem_ids:
        affected_concept_ids = [
            int(c)
            for c in (
                await db.execute(
                    select(ConceptProblem.concept_id)
                    .where(ConceptProblem.id.in_(problem_ids))
                    .distinct()
                )
            )
            .scalars()
            .all()
        ]
        res = await db.execute(
            delete(ConceptProblem).where(ConceptProblem.id.in_(problem_ids))
        )
        removed_problems = res.rowcount or 0

    doc_ids = [
        int(d)
        for d in (row.problem_document_id, row.solution_document_id)
        if d is not None
    ]
    removed_documents = 0
    if doc_ids:
        res = await db.execute(delete(AITADocument).where(AITADocument.id.in_(doc_ids)))
        removed_documents = res.rowcount or 0

    # --- Full KG teardown for concepts this set ORPHANED --------------------- #
    # STRICT / conservative: a concept is torn down ONLY if it has ZERO footprint of
    # any kind beyond this set — no remaining problems, no Postgres student/seed
    # footprint (_protected_concepts), and no :Canon RESOLVES_TO grading history.
    # Under-tearing-down only leaves KG behind; over-tearing-down would 500 or
    # destroy student data, so every ambiguous case spares the concept.
    orphaned_concept_ids: list[int] = []
    neo = None
    if affected_concept_ids:
        # PG-orphan candidates: affected concepts with NO remaining ConceptProblem
        # (the set's deletes above are visible in this uncommitted transaction).
        surviving = {
            int(c)
            for c in (
                await db.execute(
                    select(ConceptProblem.concept_id)
                    .where(ConceptProblem.concept_id.in_(affected_concept_ids))
                    .distinct()
                )
            )
            .scalars()
            .all()
        }
        protected = await _protected_concepts(
            db, [cid for cid in affected_concept_ids if cid not in surviving]
        )
        candidates = [
            cid
            for cid in affected_concept_ids
            if cid not in surviving and cid not in protected
        ]
        if candidates:
            neo = get_neo4j_client()
            with_history = await _concepts_with_canon_history(neo, candidates)
            orphaned_concept_ids = [cid for cid in candidates if cid not in with_history]
            if orphaned_concept_ids:
                # dedup_decisions FK is ON DELETE SET NULL, so delete explicitly
                # BEFORE the concept; deleting apollo_concepts cascades KGEntity and
                # apollo_entity_prereqs. apollo_sessions (the only RESTRICT FK) is
                # already spared by _protected_concepts, so this never hard-fails.
                await db.execute(
                    delete(DedupDecision).where(
                        DedupDecision.concept_id.in_(orphaned_concept_ids)
                    )
                )
                await db.execute(
                    delete(Concept).where(Concept.id.in_(orphaned_concept_ids))
                )

    await db.delete(row)
    await db.commit()

    # Neo4j shares no txn with Postgres — run the guarded :Canon teardown AFTER the
    # PG commit so a Neo4j failure cannot roll back the PG delete. Idempotent.
    if orphaned_concept_ids and neo is not None:
        await _detach_delete_canon(neo, orphaned_concept_ids)

    _LOG.info(
        "authored_set_deleted",
        extra={
            "event": "authored_set_deleted",
            "set_id": set_id,
            "removed_problems": removed_problems,
            "removed_documents": removed_documents,
            "removed_concepts": len(orphaned_concept_ids),
        },
    )
    return {
        "deleted": True,
        "removed_problems": removed_problems,
        "removed_documents": removed_documents,
        "removed_concepts": len(orphaned_concept_ids),
    }


@router.post("/authored-sets/{set_id}/problems/{problem_id}/approve")
async def approve_held_problem(
    set_id: int,
    problem_id: int,
    body: ApproveBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    authored_set = await db.get(AuthoredSet, set_id)
    if authored_set is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_teacher(
        db=db,
        auth=auth,
        search_space_id=int(authored_set.search_space_id),
    )

    row = await db.get(ConceptProblem, problem_id)
    if row is None or not _problem_belongs_to_set(authored_set, row, problem_id):
        # 404, not 403/409: don't leak whether problem_id exists at all, and
        # don't let a caller who cleared the course-membership gate use a
        # foreign set_id/problem_id pairing to promote into a search space
        # they don't control (cross-tenant IDOR — the problem must be one
        # THIS set actually minted, per its own result_summary, and must
        # live in the SAME search space as the set).
        raise HTTPException(status_code=404, detail="problem not found in this authored set")

    review = (row.provenance or {}).get("authored_review")  # type: ignore[call-overload]
    if not review or not review.get("required"):
        raise HTTPException(status_code=409, detail="problem is not held for review")

    chosen = (
        review.get("generated_alt") if body.reference == "generated" else review.get("ocr_draft")
    )
    if chosen is None:
        raise HTTPException(status_code=422, detail=f"no '{body.reference}' reference stored")
    draft = ReferenceSolutionDraft.model_validate(chosen)

    candidate = _candidate_from_row(row)
    pair = build_approved_pair(
        candidate,
        draft,
        search_space_id=int(authored_set.search_space_id),
    )
    metered_chat = _make_metered_chat(document_id=int(authored_set.problem_document_id or 0))
    mint_plan = await tag_and_mint(
        db,
        pair,
        chat_fn=_tag_mint_chat_fn(metered_chat),
        embed_fn=embed_text,
    )
    existing_hashes = await _authored_concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    result = await promote(
        db,
        get_neo4j_client(),
        problem=pair.problem,
        mint_plan=mint_plan,
        search_space_id=int(authored_set.search_space_id),
        concept_problem_id=problem_id,
        existing_problem_hashes=existing_hashes,
        solution_source=pair.solution_source,
    )
    if result.promoted:
        row.provenance = {  # type: ignore[assignment]
            **(row.provenance or {}),
            "authored_review": {
                **review,
                "required": False,
                "approved_reference": body.reference,
            },
        }
        await db.commit()
    return {
        "promoted": result.promoted,
        "failed_gate": result.failed_gate,
        "diagnostic": result.diagnostic,
    }


def _problem_belongs_to_set(
    authored_set: AuthoredSet, problem: ConceptProblem, problem_id: int
) -> bool:
    """True iff ``problem`` is one this ``authored_set`` actually minted.

    Two independent checks, both required: the id must appear in the set's own
    ``result_summary["problems"]`` list (the orchestrator's per-problem outcome
    ledger — see ``run_authored_set_provisioning`` / ``_run_set_background``),
    AND the row's own ``search_space_id`` must match the set's — belt-and-
    suspenders against a stale/corrupted ``result_summary`` pointing at a
    problem that has since moved (or was minted) into a different course.
    """
    minted_ids = {
        int(p["concept_problem_id"])
        for p in (authored_set.result_summary or {}).get("problems") or []  # type: ignore[union-attr]
        if isinstance(p, dict) and p.get("concept_problem_id") is not None
    }
    if problem_id not in minted_ids:
        return False
    return int(problem.search_space_id) == int(authored_set.search_space_id)


def _candidate_from_row(row: ConceptProblem) -> SimpleNamespace:
    payload: dict = row.payload or {}  # type: ignore[assignment]
    provenance: dict = row.provenance or {}  # type: ignore[assignment]
    return SimpleNamespace(
        problem_text=payload.get("problem_text", ""),
        given_values=payload.get("given_values", {}) or {},
        target_unknown=payload.get("target_unknown", ""),
        difficulty=payload.get("difficulty", row.difficulty),
        chunk_content_hash=provenance.get("chunk_content_hash", ""),
        concept_slug=payload.get("concept_slug", "provisional.inventory"),
        label=payload.get("label"),
        document_id=provenance.get("document_id"),
        page=provenance.get("page"),
    )
