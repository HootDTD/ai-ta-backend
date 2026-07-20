import threading
import types

import pytest

import apollo.provisioning.authored_sets.indexing as idx


def _fake_ingestion(pages):
    """A minimal ingestion result carrying per-page OCR confidence."""
    return types.SimpleNamespace(
        items=[types.SimpleNamespace(id="i1")],
        source_markdown="Problem 1 ...",
        page_count=len(pages) or 1,
        pages=pages,
        artifact_manifest={"pages": []},
        ocr_provider="openai",
        ocr_summary={"openai_pages": len(pages)},
        warning_count=0,
        warnings=[],
    )


def _patch_indexer_with_real_metadata(monkeypatch, db_session, ingestion):
    """Wire the indexer seams, but mirror the REAL prepare_for_indexing by
    copying connector metadata onto the persisted document (the production
    behavior the existing hidden-status test does not exercise)."""
    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: ingestion)
    monkeypatch.setattr(
        idx,
        "items_to_chunk_texts",
        lambda items: [("Problem 1 ...", {"page_number": 1, "chunk_type": "body"})],
    )

    async def fake_prepare(self, conn_docs):
        from database.models import Document

        cd = conn_docs[0]
        doc = Document(
            title=cd.title,
            course_id=cd.search_space_id,
            content="c",
            content_hash="h",
            unique_identifier_hash="u",
            metadata_=cd.metadata,
        )
        db_session.add(doc)
        await db_session.flush()
        return [doc]

    monkeypatch.setattr(idx.AITAIndexingService, "prepare_for_indexing", fake_prepare)

    async def fake_embed(**k):
        return 1

    async def fake_finalize(session, **k):
        return None

    monkeypatch.setattr(idx, "embed_and_persist_chunks", fake_embed)
    monkeypatch.setattr(idx, "finalize_document", fake_finalize)
    monkeypatch.setattr(idx, "embed_text", lambda t: [0.0] * 8)


def test_connector_document_includes_page_debug_confidence():
    """The connector metadata must carry per-page OCR confidence so the
    authored-set verification path can detect low-confidence pages."""
    ingestion = _fake_ingestion(
        [
            types.SimpleNamespace(page_number=1, ocr_confidence=0.3, extraction_mode="openai"),
            types.SimpleNamespace(page_number=2, ocr_confidence=0.95, extraction_mode="native"),
        ]
    )

    connector_doc = idx._connector_document(
        search_space_id=4,
        title="HW7 Solutions",
        set_index=1,
        role="solution",
        ingestion=ingestion,
    )

    page_debug = connector_doc.metadata["page_debug"]
    assert page_debug == [
        {"page": 1, "ocr_confidence": 0.3, "extraction_mode": "openai"},
        {"page": 2, "ocr_confidence": 0.95, "extraction_mode": "native"},
    ]


@pytest.mark.asyncio
async def test_index_authored_doc_sets_hidden_status(db_session, monkeypatch):
    from database.models import Course

    db_session.add(
        Course(
            id=4,
            name="AAE 333 E2E Test",
            slug="aae-333-e2e-test",
            subject_name="AAE",
        )
    )
    await db_session.flush()

    fake_ing = types.SimpleNamespace(
        items=[types.SimpleNamespace(id="i1")],
        source_markdown="Problem 1 ...",
        page_count=2,
        pages=[],
        artifact_manifest={"pages": []},
        ocr_provider="openai",
        ocr_summary={"openai_pages": 2},
        warning_count=0,
        warnings=[],
    )
    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: fake_ing)
    monkeypatch.setattr(
        idx,
        "items_to_chunk_texts",
        lambda items: [("Problem 1 ...", {"page_number": 1, "chunk_type": "body"})],
    )

    async def fake_prepare(self, docs):
        from database.models import Document

        doc = Document(
            title=docs[0].title,
            course_id=docs[0].search_space_id,
            content="c",
            content_hash="h",
            unique_identifier_hash="u",
        )
        db_session.add(doc)
        await db_session.flush()
        return [doc]

    monkeypatch.setattr(idx.AITAIndexingService, "prepare_for_indexing", fake_prepare)

    async def fake_embed(**k):
        return 1

    monkeypatch.setattr(idx, "embed_and_persist_chunks", fake_embed)

    async def fake_finalize(session, **k):
        return None

    monkeypatch.setattr(idx, "finalize_document", fake_finalize)
    monkeypatch.setattr(idx, "embed_text", lambda t: [0.0] * 8)

    doc_id = await idx.index_authored_doc(
        db_session,
        search_space_id=4,
        file_bytes=b"%PDF-1.4 fake",
        title="HW7 Problems",
        set_index=1,
        role="problem",
    )

    from database.models import Document

    doc = await db_session.get(Document, doc_id)
    assert doc.status == "queued"


@pytest.mark.asyncio
async def test_index_authored_doc_propagates_page_confidence(db_session, monkeypatch):
    """End-to-end indexer seam: a low-confidence page must surface through
    ``chunk_ocr_confidence`` so the orchestrator's OCR cross-check can fire."""
    from apollo.provisioning.authored_sets.paired_retrieval import chunk_ocr_confidence
    from database.models import Course

    db_session.add(Course(id=7, name="AAE Conf", slug="aae-conf", subject_name="AAE"))
    await db_session.flush()

    ingestion = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.3, extraction_mode="openai")]
    )
    _patch_indexer_with_real_metadata(monkeypatch, db_session, ingestion)

    doc_id = await idx.index_authored_doc(
        db_session,
        search_space_id=7,
        file_bytes=b"%PDF-1.4 fake",
        title="HW7 Solutions",
        set_index=1,
        role="solution",
    )

    page_conf = await chunk_ocr_confidence(db_session, document_id=doc_id)
    assert page_conf == {1: 0.3}


@pytest.mark.asyncio
async def test_index_authored_doc_captures_pages_into_sink(db_session, monkeypatch):
    """The optional ``page_sink`` receives the transient per-page OCR pass so the
    caller can persist page-level OCR evidence (WU-AAS observability)."""
    from database.models import Course

    db_session.add(Course(id=9, name="Sink", slug="sink", subject_name="AAE"))
    await db_session.flush()

    pages = [
        types.SimpleNamespace(page_number=1, ocr_confidence=0.8, extraction_mode="openai")
    ]
    ingestion = _fake_ingestion(pages)
    _patch_indexer_with_real_metadata(monkeypatch, db_session, ingestion)

    sink: list = []
    await idx.index_authored_doc(
        db_session,
        search_space_id=9,
        file_bytes=b"%PDF-1.4 fake",
        title="Sink Set",
        set_index=1,
        role="problem",
        page_sink=sink,
    )
    assert sink == pages


@pytest.mark.asyncio
async def test_index_authored_doc_rejects_bad_role(db_session):
    with pytest.raises(ValueError, match="role must be"):
        await idx.index_authored_doc(
            db_session,
            search_space_id=1,
            file_bytes=b"%PDF",
            title="t",
            set_index=1,
            role="bogus",
        )


@pytest.mark.asyncio
async def test_index_authored_doc_raises_on_no_items(db_session, monkeypatch):
    ing = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.9, extraction_mode="native")]
    )
    ing.items = []
    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: ing)
    with pytest.raises(ValueError, match="no chunks produced"):
        await idx.index_authored_doc(
            db_session,
            search_space_id=1,
            file_bytes=b"%PDF",
            title="t",
            set_index=1,
            role="problem",
        )


@pytest.mark.asyncio
async def test_index_authored_doc_raises_on_no_chunk_texts(db_session, monkeypatch):
    from database.models import Course

    db_session.add(Course(id=9, name="x", slug="aae-nochunks", subject_name="AAE"))
    await db_session.flush()
    ing = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.9, extraction_mode="native")]
    )
    _patch_indexer_with_real_metadata(monkeypatch, db_session, ing)
    monkeypatch.setattr(idx, "items_to_chunk_texts", lambda items: [])
    with pytest.raises(ValueError, match="no chunk texts"):
        await idx.index_authored_doc(
            db_session,
            search_space_id=9,
            file_bytes=b"%PDF",
            title="t",
            set_index=1,
            role="problem",
        )


@pytest.mark.asyncio
async def test_index_authored_doc_falls_back_to_existing_doc(db_session, monkeypatch):
    from database.models import Document, Course
    from indexing.document_hashing import compute_unique_identifier_hash

    db_session.add(Course(id=11, name="x", slug="aae-existing", subject_name="AAE"))
    await db_session.flush()
    ing = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.9, extraction_mode="native")]
    )
    # Mirror the connector doc the indexer will build so the unique hash matches.
    connector = idx._connector_document(
        search_space_id=11, title="t", set_index=1, role="problem", ingestion=ing
    )
    existing = Document(
        title="t",
        content="c",
        content_hash="h-existing",
        course_id=11,
        unique_identifier_hash=compute_unique_identifier_hash(connector),
        status="ready",
    )
    db_session.add(existing)
    await db_session.flush()
    existing_id = int(existing.id)

    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: ing)

    async def empty_prepare(self, conn_docs):
        return []

    monkeypatch.setattr(idx.AITAIndexingService, "prepare_for_indexing", empty_prepare)

    doc_id = await idx.index_authored_doc(
        db_session,
        search_space_id=11,
        file_bytes=b"%PDF",
        title="t",
        set_index=1,
        role="problem",
    )
    assert doc_id == existing_id
    refreshed = await db_session.get(Document, existing_id)
    assert refreshed.status == "queued"


@pytest.mark.asyncio
async def test_reupload_reuses_content_identical_doc_via_real_prepare(db_session, monkeypatch):
    """Regression for "prepare_for_indexing returned no doc".

    Runs the REAL ``prepare_for_indexing`` (not a stub). A first upload's doc is
    already indexed; a re-upload of identical bytes mints a fresh ``set_index``
    (new ``unique_id``) but the SAME content_hash. The real service dedups it
    away (returns []) and the indexer must reuse the content-identical doc by
    falling back to the content hash — not raise.
    """
    from database.models import Document, Course
    from indexing.document_hashing import (
        compute_content_hash,
        compute_unique_identifier_hash,
    )

    db_session.add(Course(id=21, name="x", slug="aae-reupload", subject_name="AAE"))
    await db_session.flush()
    ing = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.9, extraction_mode="native")]
    )

    # The doc the FIRST upload (set_index=1) indexed and committed before failing.
    first = idx._connector_document(
        search_space_id=21, title="t", set_index=1, role="problem", ingestion=ing
    )
    existing = Document(
        title="t",
        content="c",
        content_hash=compute_content_hash(first),
        course_id=21,
        unique_identifier_hash=compute_unique_identifier_hash(first),
        status="queued",
    )
    db_session.add(existing)
    await db_session.flush()
    existing_id = int(existing.id)

    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: ing)

    # Re-upload as a NEW set_index — real prepare_for_indexing dedups on content.
    doc_id = await idx.index_authored_doc(
        db_session,
        search_space_id=21,
        file_bytes=b"%PDF",
        title="t",
        set_index=2,
        role="problem",
    )
    assert doc_id == existing_id


@pytest.mark.asyncio
async def test_index_authored_doc_raises_when_no_doc_and_no_existing(db_session, monkeypatch):
    from database.models import Course

    db_session.add(Course(id=12, name="x", slug="aae-nodoc", subject_name="AAE"))
    await db_session.flush()
    ing = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.9, extraction_mode="native")]
    )
    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: ing)

    async def empty_prepare(self, conn_docs):
        return []

    monkeypatch.setattr(idx.AITAIndexingService, "prepare_for_indexing", empty_prepare)
    with pytest.raises(RuntimeError, match="returned no doc"):
        await idx.index_authored_doc(
            db_session,
            search_space_id=12,
            file_bytes=b"%PDF",
            title="t",
            set_index=1,
            role="problem",
        )


@pytest.mark.asyncio
async def test_index_authored_doc_offloads_ingest_off_event_loop(db_session, monkeypatch):
    """The blocking PyMuPDF/OCR ingest must run off the event-loop thread so it
    never stalls concurrent request handling."""
    from database.models import Course

    db_session.add(Course(id=8, name="AAE Thread", slug="aae-thread", subject_name="AAE"))
    await db_session.flush()

    loop_thread_ident = threading.get_ident()
    captured: dict[str, int] = {}

    ingestion = _fake_ingestion(
        [types.SimpleNamespace(page_number=1, ocr_confidence=0.9, extraction_mode="native")]
    )

    def capturing_ingest(*a, **k):
        captured["ident"] = threading.get_ident()
        return ingestion

    _patch_indexer_with_real_metadata(monkeypatch, db_session, ingestion)
    monkeypatch.setattr(idx, "_run_ingest", capturing_ingest)

    await idx.index_authored_doc(
        db_session,
        search_space_id=8,
        file_bytes=b"%PDF-1.4 fake",
        title="HW7 Solutions",
        set_index=1,
        role="solution",
    )

    assert captured["ident"] != loop_thread_ident
