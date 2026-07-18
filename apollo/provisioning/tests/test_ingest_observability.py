"""WU-AAS ingestion observability (lane B2.4 / G4.4).

The authored-set ingestion path used to leave apollo_ingest_runs /
apollo_ingest_errors EMPTY and persist no per-page OCR text, so the S2 audit ran
on thin inputs. These tests pin the new observability writes: an ingest run row
per ingestion, per-page OCR evidence (text + confidence + verify-path flag), a
stage-error row on failure, and the GET surface that exposes it all.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from apollo.persistence.models import IngestError, IngestPageEvidence, IngestRun


def _page(page_number, *, plain="", latex="", conf=None, mode="ocr"):
    """A NormalizedPage-shaped stand-in for one ingested page."""
    return SimpleNamespace(
        page_number=page_number,
        plain_text=plain,
        latex_text=latex,
        ocr_confidence=conf,
        extraction_mode=mode,
    )


async def _seed_course(db, *, slug: str) -> tuple[int, int]:
    from apollo.persistence.models import Concept, Subject
    from database.models import Course

    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
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


# ---------------------------------------------------------------------------
# observability module — unit-level DB writes
# ---------------------------------------------------------------------------


def test_page_ocr_text_combines_plain_and_latex():
    from apollo.provisioning.authored_sets.observability import page_ocr_text

    assert page_ocr_text(_page(1, plain="Find v.", latex="$v = at$")) == "Find v.\n\n$v = at$"
    assert page_ocr_text(_page(1, plain="only plain")) == "only plain"
    assert page_ocr_text(_page(1)) == ""


@pytest.mark.asyncio
async def test_start_ingest_run_opens_running_row(db_session):
    from apollo.provisioning.authored_sets.observability import start_ingest_run

    space_id, _c = await _seed_course(db_session, slug="obs-start")
    run = await start_ingest_run(
        db_session, search_space_id=space_id, document_id=555, content_hash="h1"
    )
    assert run.id is not None
    assert run.status == "running"
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.document_id == 555
    assert run.content_hash == "h1"


@pytest.mark.asyncio
async def test_persist_page_evidence_writes_text_and_confidence(db_session):
    from sqlalchemy import select

    from apollo.provisioning.authored_sets.observability import (
        persist_page_evidence,
        start_ingest_run,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-pages")
    run = await start_ingest_run(db_session, search_space_id=space_id, document_id=700)
    pages = [
        _page(1, plain="Problem 1.", latex="$x=1$", conf=0.95, mode="ocr"),
        _page(2, plain="Problem 2.", conf=0.9, mode="native"),
    ]
    n = await persist_page_evidence(
        db_session,
        ingest_run=run,
        search_space_id=space_id,
        document_id=700,
        role="problem",
        pages=pages,
    )
    assert n == 2

    rows = (
        (
            await db_session.execute(
                select(IngestPageEvidence)
                .where(IngestPageEvidence.ingest_run_id == run.id)
                .order_by(IngestPageEvidence.page_number.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [r.page_number for r in rows] == [1, 2]
    assert rows[0].ocr_text == "Problem 1.\n\n$x=1$"
    assert rows[0].ocr_confidence == pytest.approx(0.95)
    assert rows[0].extraction_mode == "ocr"
    assert rows[0].role == "problem"
    assert rows[0].document_id == 700
    # High-confidence pages do NOT trip the verify path.
    assert rows[0].verify_path_fired is False
    assert rows[1].verify_path_fired is False


@pytest.mark.asyncio
async def test_persist_page_evidence_flags_low_confidence_verify_path(db_session):
    from sqlalchemy import select

    from apollo.provisioning.authored_sets.observability import (
        persist_page_evidence,
        start_ingest_run,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-lowconf")
    run = await start_ingest_run(db_session, search_space_id=space_id, document_id=800)
    pages = [
        _page(1, plain="legible", conf=0.9),  # above threshold -> no verify
        _page(2, plain="messy handwriting", conf=0.4),  # below 0.6 -> verify
        _page(3, plain="native text", conf=None),  # no confidence -> no verify
    ]
    await persist_page_evidence(
        db_session,
        ingest_run=run,
        search_space_id=space_id,
        document_id=800,
        role="solution",
        pages=pages,
    )
    rows = (
        (
            await db_session.execute(
                select(IngestPageEvidence)
                .where(IngestPageEvidence.ingest_run_id == run.id)
                .order_by(IngestPageEvidence.page_number.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [r.verify_path_fired for r in rows] == [False, True, False]


@pytest.mark.asyncio
async def test_finalize_ingest_run_stamps_status_and_counts(db_session):
    from apollo.provisioning.authored_sets.observability import (
        finalize_ingest_run,
        start_ingest_run,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-final")
    run = await start_ingest_run(db_session, search_space_id=space_id, document_id=900)
    await finalize_ingest_run(
        db_session,
        ingest_run=run,
        status="succeeded",
        n_pages=4,
        n_questions_scraped=3,
        n_promoted=2,
        n_rejected=1,
    )
    assert run.status == "succeeded"
    assert run.finished_at is not None
    assert run.n_pages == 4
    assert run.n_questions_scraped == 3
    assert run.n_promoted == 2
    assert run.n_rejected == 1


@pytest.mark.asyncio
async def test_record_ingest_error_writes_row(db_session):
    from sqlalchemy import select

    from apollo.provisioning.authored_sets.observability import (
        record_ingest_error,
        start_ingest_run,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-err")
    run = await start_ingest_run(db_session, search_space_id=space_id, document_id=1000)
    await record_ingest_error(
        db_session,
        search_space_id=space_id,
        ingest_run=run,
        stage="authored_set_ingest",
        exc=ValueError("no chunks produced"),
        context={"set_id": 7},
    )
    errors = (
        (await db_session.execute(select(IngestError).where(IngestError.ingest_run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(errors) == 1
    assert errors[0].stage == "authored_set_ingest"
    assert errors[0].error_class == "ValueError"
    assert errors[0].context["set_id"] == 7
    assert "no chunks produced" in errors[0].context["message"]


# ---------------------------------------------------------------------------
# end-to-end through the API background task + GET exposure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_background_run_populates_ingest_run_and_page_evidence(db_session, monkeypatch):
    from sqlalchemy import select

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet
    from apollo.provisioning.authored_sets.orchestrator import (
        ProblemResult,
        ProvisioningReport,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-e2e")
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, page_sink=None, **_kwargs):
        # Simulate the transient per-page OCR pass the real indexer captures.
        if role == "problem":
            page_sink.append(_page(1, plain="Problem 1(a).", conf=0.95))
            return 201
        page_sink.append(_page(1, plain="Solution 1(a).", conf=0.5))  # low -> verify
        return 202

    async def _run_provisioning(db, neo, **kwargs):
        return ProvisioningReport(
            problems=[ProblemResult(label="1(a)", outcome="promoted")],
            counts={"promoted": 1, "rejected": 0},
        )

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "run_authored_set_provisioning", _run_provisioning)
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="problems.pdf",
        solution_bytes=b"%PDF s",
        solution_title="solutions.pdf",
    )

    run = (
        await db_session.execute(select(IngestRun).where(IngestRun.document_id == 201))
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.n_pages == 2
    assert run.n_questions_scraped == 1
    assert run.n_promoted == 1
    assert run.finished_at is not None

    evidence = (
        (
            await db_session.execute(
                select(IngestPageEvidence)
                .where(IngestPageEvidence.ingest_run_id == run.id)
                .order_by(IngestPageEvidence.id.asc())
            )
        )
        .scalars()
        .all()
    )
    assert {e.role for e in evidence} == {"problem", "solution"}
    by_role = {e.role: e for e in evidence}
    assert by_role["problem"].ocr_text == "Problem 1(a)."
    assert by_role["problem"].verify_path_fired is False
    assert by_role["solution"].verify_path_fired is True


@pytest.mark.asyncio
async def test_get_authored_set_exposes_ingest_run_and_pages(db_session, monkeypatch):
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet
    from apollo.provisioning.authored_sets.observability import (
        finalize_ingest_run,
        persist_page_evidence,
        start_ingest_run,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-get")
    run = await start_ingest_run(db_session, search_space_id=space_id, document_id=301)
    await persist_page_evidence(
        db_session,
        ingest_run=run,
        search_space_id=space_id,
        document_id=301,
        role="problem",
        pages=[_page(1, plain="Q1", conf=0.4)],
    )
    await finalize_ingest_run(
        db_session, ingest_run=run, status="succeeded", n_pages=1, n_promoted=1
    )
    aset = AuthoredSet(
        search_space_id=space_id,
        set_index=1,
        status="done",
        problem_document_id=301,
        solution_document_id=302,
        result_summary={"counts": {"promoted": 1}},
    )
    db_session.add(aset)
    await db_session.flush()

    async def _fake_require_user(_request):
        from auth import AuthContext

        return AuthContext(user_id="teacher-1", access_token="token")

    async def _fake_member(**_kwargs):
        return None

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", _fake_member)

    detail = await aapi.get_authored_set(
        set_id=int(aset.id), request=SimpleNamespace(), db=db_session
    )
    assert detail["ingest_run"]["status"] == "succeeded"
    assert detail["ingest_run"]["n_pages"] == 1
    assert detail["ingest_run"]["n_promoted"] == 1
    assert len(detail["pages"]) == 1
    page = detail["pages"][0]
    assert page["page_ref"] == "problem:p1"
    assert page["ocr_text"] == "Q1"
    assert page["ocr_text_truncated"] is False
    assert page["ocr_text_chars"] == 2
    assert page["ocr_confidence"] == pytest.approx(0.4)
    assert page["verify_path_fired"] is True


@pytest.mark.asyncio
async def test_get_authored_set_caps_ocr_text_unless_full_ocr(db_session, monkeypatch):
    """The GET list surface truncates each page's ocr_text to _LIST_OCR_TEXT_CAP
    (flagging it) so a run of long pages doesn't bloat the payload; ?full_ocr=true
    returns the untruncated body for a deliberate deep fetch."""
    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet
    from apollo.provisioning.authored_sets.observability import (
        persist_page_evidence,
        start_ingest_run,
    )

    space_id, _c = await _seed_course(db_session, slug="obs-cap")
    long_text = "x" * (aapi._LIST_OCR_TEXT_CAP + 500)
    run = await start_ingest_run(db_session, search_space_id=space_id, document_id=311)
    await persist_page_evidence(
        db_session,
        ingest_run=run,
        search_space_id=space_id,
        document_id=311,
        role="problem",
        pages=[_page(1, plain=long_text, conf=0.9)],
    )
    aset = AuthoredSet(
        search_space_id=space_id, set_index=1, status="done", problem_document_id=311
    )
    db_session.add(aset)
    await db_session.flush()

    async def _fake_require_user(_request):
        from auth import AuthContext

        return AuthContext(user_id="teacher-1", access_token="token")

    monkeypatch.setattr(aapi, "require_user", _fake_require_user)
    monkeypatch.setattr(aapi, "require_course_teacher", lambda **_kwargs: _noop())

    capped = await aapi.get_authored_set(
        set_id=int(aset.id), request=SimpleNamespace(), db=db_session
    )
    page = capped["pages"][0]
    assert len(page["ocr_text"]) == aapi._LIST_OCR_TEXT_CAP
    assert page["ocr_text_truncated"] is True
    assert page["ocr_text_chars"] == len(long_text)

    full = await aapi.get_authored_set(
        set_id=int(aset.id), request=SimpleNamespace(), full_ocr=True, db=db_session
    )
    full_page = full["pages"][0]
    assert full_page["ocr_text"] == long_text
    assert full_page["ocr_text_truncated"] is False


async def _noop():
    return None


@pytest.mark.asyncio
async def test_background_failure_marks_run_failed_and_records_error(db_session, monkeypatch):
    from sqlalchemy import select

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _c = await _seed_course(db_session, slug="obs-fail")
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, page_sink=None, **_kwargs):
        page_sink.append(_page(1, plain="page", conf=0.9))
        return 401 if role == "problem" else 402

    async def _run_provisioning(db, neo, **_kwargs):
        raise RuntimeError("provisioning boom")

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "run_authored_set_provisioning", _run_provisioning)
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="p",
        solution_bytes=b"%PDF s",
        solution_title="s",
    )

    refreshed = await db_session.get(AuthoredSet, set_id)
    assert refreshed.status == "failed"

    run = (
        await db_session.execute(select(IngestRun).where(IngestRun.document_id == 401))
    ).scalar_one()
    assert run.status == "failed"
    assert run.finished_at is not None

    errors = (
        (await db_session.execute(select(IngestError).where(IngestError.ingest_run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(errors) == 1
    assert errors[0].stage == "authored_set_ingest"
    assert "provisioning boom" in errors[0].context["message"]


@pytest.mark.asyncio
async def test_background_indexing_failure_opens_run_and_persists_evidence(db_session, monkeypatch):
    """An indexing-stage failure (bad PDF / no chunks produced) must still leave a
    run row + error + whatever page evidence was captured — the S2 'insufficient
    info' failure class. The run is opened BEFORE indexing, so a raise from
    ``index_authored_doc`` cannot leave both observability tables empty."""
    from sqlalchemy import select

    import apollo.provisioning.authored_sets.api as aapi
    from apollo.persistence.models import AuthoredSet

    space_id, _c = await _seed_course(db_session, slug="obs-idxfail")
    row = AuthoredSet(search_space_id=space_id, set_index=1, status="pending")
    db_session.add(row)
    await db_session.flush()
    set_id = int(row.id)

    @asynccontextmanager
    async def _session_cm():
        yield db_session

    async def _index_authored_doc(db, *, role, page_sink=None, **_kwargs):
        # The real indexer appends the transient per-page OCR pass BEFORE the
        # no-items guard, then raises "no chunks produced" — so the sink holds a
        # page even though indexing failed and NO document id was ever minted.
        page_sink.append(_page(1, plain="captured before failure", conf=0.4))
        raise ValueError("authored indexer: no chunks produced from problem PDF")

    def _fail_provisioning(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("provisioning must not run after an indexing failure")

    monkeypatch.setattr(aapi, "get_async_session", _session_cm)
    monkeypatch.setattr(aapi, "index_authored_doc", _index_authored_doc)
    monkeypatch.setattr(aapi, "run_authored_set_provisioning", _fail_provisioning)
    monkeypatch.setattr(aapi, "_make_metered_chat", lambda **_kwargs: "metered")
    monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")

    await aapi._run_set_background(
        set_id=set_id,
        search_space_id=space_id,
        set_index=1,
        problem_bytes=b"%PDF p",
        problem_title="p",
        solution_bytes=b"%PDF s",
        solution_title="s",
    )

    refreshed = await db_session.get(AuthoredSet, set_id)
    assert refreshed.status == "failed"
    assert "no chunks produced" in (refreshed.result_summary or {}).get("error", "")

    # The run row EXISTS (opened before indexing) and is marked failed, even though
    # indexing never produced a document id.
    run = (
        await db_session.execute(
            select(IngestRun)
            .where(IngestRun.search_space_id == space_id)
            .order_by(IngestRun.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.document_id is None  # problem indexing failed before minting a doc

    # The stage error is recorded against the now-existing run.
    errors = (
        (await db_session.execute(select(IngestError).where(IngestError.ingest_run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(errors) == 1
    assert errors[0].stage == "authored_set_ingest"
    assert "no chunks produced" in errors[0].context["message"]

    # The page evidence captured before the raise is persisted (surfacing the OCR
    # inputs the failure-path comment in indexing.py promises).
    evidence = (
        (
            await db_session.execute(
                select(IngestPageEvidence).where(IngestPageEvidence.ingest_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(evidence) == 1
    assert evidence[0].role == "problem"
    assert evidence[0].ocr_text == "captured before failure"
    assert evidence[0].verify_path_fired is True
