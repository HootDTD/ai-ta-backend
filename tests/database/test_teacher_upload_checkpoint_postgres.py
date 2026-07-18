from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from database.models import (
    Document,
    DocumentStatus,
    Course,
    Upload,
    UploadJob,
)
from indexing.connector_document import AITAConnectorDocument
from knowledge.teacher_weekly import (
    JOB_STATE_PROCESSING,
    UPLOAD_STATUS_PROCESSING,
    ClaimedUploadJob,
    TeacherWeeklyStorage,
)
from tests.fakes.embeddings import fake_embedding

pytestmark = pytest.mark.integration


class _FakeIngestion:
    page_count = 2
    warning_count = 0
    ocr_provider = "native"
    ocr_summary: dict = {}
    artifact_manifest: dict = {"pages": [{"page": 1}, {"page": 2}]}
    source_markdown = "Textbook body"
    items: list = []


class _NoopStorage:
    def ensure_bucket(self, **k): ...
    def upload_bytes(self, **k): ...
    def download_bytes(self, **k):
        return b""


async def _seed(db_session):
    space = Course(name="Fluids", slug="fluids-ck-e2e", subject_name="ME")
    db_session.add(space)
    await db_session.flush()
    upload = Upload(
        course_id=space.id,
        week=0,
        kind="textbook",
        title="Textbook",
        source_name="t.pdf",
        status=UPLOAD_STATUS_PROCESSING,
        storage_key="k",
        started_at=datetime.now(UTC),
        artifact_manifest={
            "bucket": "b",
            "pages": [],
            "embed_progress": {"last_completed_page": 1},
        },  # resume from page 1
    )
    db_session.add(upload)
    await db_session.flush()
    job = UploadJob(
        course_id=space.id,
        upload_id=upload.id,
        state=JOB_STATE_PROCESSING,
        lease_owner="w1",
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),  # already expired
        attempt_count=2,
    )
    db_session.add(job)
    await db_session.commit()
    return space, upload, job


async def test_index_existing_upload_resumes_and_finalizes(db_session, monkeypatch):
    space, upload, job = await _seed(db_session)

    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    monkeypatch.setattr("knowledge.teacher_weekly.get_async_session", _factory)
    # Deterministic, correctly-dimensioned vectors everywhere they are looked up:
    monkeypatch.setattr(
        "indexing.checkpoint_indexer.embed_texts", lambda texts: [fake_embedding(t) for t in texts]
    )
    monkeypatch.setattr(
        "indexing.document_embedder.embed_text", lambda text, **k: fake_embedding(text)
    )
    # Two pages of chunks; page 1 should be skipped (pointer=1), page 2 embedded.
    chunk_pairs = [
        ("p1", {"page_number": 1, "chunk_type": "body"}),
        ("p2", {"page_number": 2, "chunk_type": "body"}),
    ]
    monkeypatch.setattr("indexing.document_chunker.items_to_chunk_texts", lambda items: chunk_pairs)

    storage = TeacherWeeklyStorage(storage_client=_NoopStorage())
    connector = AITAConnectorDocument(
        title="Textbook",
        source_markdown="body",
        unique_id=f"teacher-upload:{upload.id}",
        search_space_id=space.id,
        material_kind="textbook",
        week=None,
    )
    claimed = ClaimedUploadJob(
        job_id=job.id,
        upload_id=upload.id,
        search_space_id=space.id,
        week=0,
        kind="textbook",
        title="Textbook",
        source_name="t.pdf",
        storage_key="k",
    )
    await storage._index_existing_upload_async(
        claimed=claimed,
        connector_doc=connector,
        items=[],
        ingestion=_FakeIngestion(),
        source_sha256="sha",
    )

    refreshed_upload = await db_session.get(Upload, upload.id)
    await db_session.refresh(refreshed_upload)
    assert refreshed_upload.status == "ready"
    assert refreshed_upload.document_id is not None
    refreshed_job = await db_session.get(UploadJob, job.id)
    await db_session.refresh(refreshed_job)
    assert refreshed_job.attempt_count == 0  # reset on progress


async def test_index_existing_upload_resolves_existing_document(db_session, monkeypatch):
    """When prepare_for_indexing returns [] (content already indexed), the worker
    resolves the existing document by hash, sets it processing, and finalizes it ready."""
    from indexing.document_hashing import compute_content_hash, compute_unique_identifier_hash

    space, upload, job = await _seed(db_session)

    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    monkeypatch.setattr("knowledge.teacher_weekly.get_async_session", _factory)
    monkeypatch.setattr(
        "indexing.checkpoint_indexer.embed_texts", lambda texts: [fake_embedding(t) for t in texts]
    )
    monkeypatch.setattr(
        "indexing.document_embedder.embed_text", lambda text, **k: fake_embedding(text)
    )
    chunk_pairs = [
        ("p1", {"page_number": 1, "chunk_type": "body"}),
        ("p2", {"page_number": 2, "chunk_type": "body"}),
    ]
    monkeypatch.setattr("indexing.document_chunker.items_to_chunk_texts", lambda items: chunk_pairs)

    connector = AITAConnectorDocument(
        title="Textbook",
        source_markdown="body",
        unique_id=f"teacher-upload:{upload.id}",
        search_space_id=space.id,
        material_kind="textbook",
        week=None,
    )
    # Pre-seed a READY doc whose hashes match the connector -> prepare_for_indexing
    # returns [] and the worker takes the existing-document resolution branch.
    existing = Document(
        title="Textbook",
        content="old",
        course_id=space.id,
        content_hash=compute_content_hash(connector),
        unique_identifier_hash=compute_unique_identifier_hash(connector),
        status=DocumentStatus.ready(),
    )
    db_session.add(existing)
    await db_session.commit()

    storage = TeacherWeeklyStorage(storage_client=_NoopStorage())
    claimed = ClaimedUploadJob(
        job_id=job.id,
        upload_id=upload.id,
        search_space_id=space.id,
        week=0,
        kind="textbook",
        title="Textbook",
        source_name="t.pdf",
        storage_key="k",
    )
    await storage._index_existing_upload_async(
        claimed=claimed,
        connector_doc=connector,
        items=[],
        ingestion=_FakeIngestion(),
        source_sha256="sha",
    )

    refreshed = await db_session.get(Document, existing.id)
    await db_session.refresh(refreshed)
    assert DocumentStatus.is_state(refreshed.status, DocumentStatus.READY)
    refreshed_upload = await db_session.get(Upload, upload.id)
    await db_session.refresh(refreshed_upload)
    assert refreshed_upload.document_id == existing.id


async def test_index_existing_upload_raises_on_empty_chunks(db_session, monkeypatch):
    """Empty chunk extraction must raise a clear ValueError (the empty-chunks guard)."""
    space, upload, job = await _seed(db_session)

    @contextlib.asynccontextmanager
    async def _factory():
        yield db_session

    monkeypatch.setattr("knowledge.teacher_weekly.get_async_session", _factory)
    monkeypatch.setattr("indexing.document_chunker.items_to_chunk_texts", lambda items: [])

    connector = AITAConnectorDocument(
        title="Textbook",
        source_markdown="body",
        unique_id=f"teacher-upload:{upload.id}",
        search_space_id=space.id,
        material_kind="textbook",
        week=None,
    )
    storage = TeacherWeeklyStorage(storage_client=_NoopStorage())
    claimed = ClaimedUploadJob(
        job_id=job.id,
        upload_id=upload.id,
        search_space_id=space.id,
        week=0,
        kind="textbook",
        title="Textbook",
        source_name="t.pdf",
        storage_key="k",
    )
    with pytest.raises(ValueError, match="No chunk texts"):
        await storage._index_existing_upload_async(
            claimed=claimed,
            connector_doc=connector,
            items=[],
            ingestion=_FakeIngestion(),
            source_sha256="sha",
        )
