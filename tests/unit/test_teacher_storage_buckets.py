"""Bucket auto-ensure and idempotent page-asset uploads for teacher uploads.

Staging failure #2: the test Supabase project had zero storage buckets (prod's
were created by hand and never mirrored), so the first upload 500'd. Failure
follow-up: worker retries re-upload existing page PNGs with ``x-upsert: false``
and collect hundreds of duplicate-object warnings. These tests pin the fixes:
``ensure_bucket`` on the storage client, a memoized ``_ensure_buckets()``
before first storage use, and ``upsert=True`` for page assets.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge.teacher_weekly import TeacherWeeklyStorage
from vendors.supabase_storage import SupabaseStorageClient

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
class _RecordingClient:
    """Storage client double that records every call."""

    def __init__(self) -> None:
        self.ensured: list[tuple[str, bool]] = []
        self.uploads: list[dict] = []
        self.downloads: list[dict] = []

    def ensure_bucket(self, *, bucket: str, public: bool = False) -> None:
        self.ensured.append((bucket, public))

    def upload_bytes(self, **kwargs) -> None:
        self.uploads.append(kwargs)

    def download_bytes(self, **kwargs) -> bytes:
        self.downloads.append(kwargs)
        return b"%PDF-1.4"


class _LegacyClient:
    """A client double without ensure_bucket (pre-existing injected fakes)."""

    def upload_bytes(self, **kwargs) -> None:
        pass


def _storage(tmp_path, client) -> TeacherWeeklyStorage:
    return TeacherWeeklyStorage(base_dir=tmp_path, total_weeks=3, storage_client=client)


# --------------------------------------------------------------------------- #
# _store_page_asset — upsert + ensure
# --------------------------------------------------------------------------- #
def test_store_page_asset_uploads_with_upsert(tmp_path):
    """Retries must overwrite existing page PNGs instead of warning-spamming."""
    client = _RecordingClient()
    storage = _storage(tmp_path, client)

    storage._store_page_asset(upload_id=2, page_number=1, image_bytes=b"png", width=100, height=200)

    [upload] = client.uploads
    assert upload["upsert"] is True
    assert upload["bucket"] == storage.pages_bucket
    assert upload["content_type"] == "image/png"


def test_store_page_asset_ensures_buckets_before_uploading(tmp_path):
    client = _RecordingClient()
    storage = _storage(tmp_path, client)

    storage._store_page_asset(upload_id=2, page_number=1, image_bytes=b"png", width=100, height=200)

    assert (storage.upload_bucket, False) in client.ensured
    assert (storage.pages_bucket, False) in client.ensured


# --------------------------------------------------------------------------- #
# _ensure_buckets — memoization + legacy-client tolerance
# --------------------------------------------------------------------------- #
def test_ensure_buckets_runs_once_per_instance(tmp_path):
    client = _RecordingClient()
    storage = _storage(tmp_path, client)

    storage._ensure_buckets()
    storage._ensure_buckets()

    assert len(client.ensured) == 2  # one call per bucket, not per invocation


def test_ensure_buckets_tolerates_clients_without_ensure_support(tmp_path):
    storage = _storage(tmp_path, _LegacyClient())

    storage._ensure_buckets()  # must not raise


# --------------------------------------------------------------------------- #
# _upload_source_pdf / _download_source_pdf — ensure-first seams
# --------------------------------------------------------------------------- #
def test_upload_source_pdf_ensures_then_uploads_to_upload_bucket(tmp_path):
    client = _RecordingClient()
    storage = _storage(tmp_path, client)

    storage._upload_source_pdf(storage_key="space/wk/key.pdf", payload=b"%PDF-1.4")

    assert client.ensured, "buckets must be ensured before the first upload"
    [upload] = client.uploads
    assert upload["bucket"] == storage.upload_bucket
    assert upload["object_key"] == "space/wk/key.pdf"
    assert upload["content_type"] == "application/pdf"


def test_download_source_pdf_ensures_then_downloads_from_upload_bucket(tmp_path):
    client = _RecordingClient()
    storage = _storage(tmp_path, client)

    payload = storage._download_source_pdf(storage_key="space/wk/key.pdf")

    assert client.ensured, "buckets must be ensured before the first download"
    assert payload == b"%PDF-1.4"
    [download] = client.downloads
    assert download["bucket"] == storage.upload_bucket
    assert download["object_key"] == "space/wk/key.pdf"


# --------------------------------------------------------------------------- #
# Wiring: the worker and enqueue paths go through the ensure-first seams
# --------------------------------------------------------------------------- #
def test_worker_job_downloads_source_pdf_via_ensure_first_seam(tmp_path, monkeypatch):
    """_process_claimed_upload_job must fetch the PDF through _download_source_pdf."""
    client = _RecordingClient()
    storage = _storage(tmp_path, client)
    claimed = SimpleNamespace(
        job_id=1,
        upload_id=2,
        search_space_id=2,
        week=0,
        kind="textbook",
        title="Textbook",
        source_name="fluidMechanics.pdf",
        storage_key="space/wk/key.pdf",
    )
    ingested = (object(), SimpleNamespace(items=[object()]), "sha256")

    async def fake_marker(upload_id):
        return None

    async def fake_index(**kwargs):
        return None

    monkeypatch.setattr(storage, "_get_reindex_marker_async", fake_marker)
    monkeypatch.setattr(
        storage, "_ingest_pdf_upload", lambda *, claimed, pdf_path, reindex_marker: ingested
    )
    monkeypatch.setattr(storage, "_index_existing_upload_async", fake_index)

    storage._process_claimed_upload_job(claimed)

    assert client.ensured, "buckets must be ensured before the worker download"
    [download] = client.downloads
    assert download == {"bucket": storage.upload_bucket, "object_key": "space/wk/key.pdf"}


class _FakeSession:
    """Minimal async-session double for the enqueue path (add/flush/commit/refresh)."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = 7

    async def commit(self) -> None:
        pass

    async def refresh(self, obj) -> None:
        pass


def test_enqueue_uploads_source_pdf_via_ensure_first_seam(tmp_path, monkeypatch):
    """enqueue_upload_by_search_space must ship the PDF through _upload_source_pdf."""
    import contextlib

    import knowledge.teacher_weekly as tw

    client = _RecordingClient()
    storage = _storage(tmp_path, client)
    pdf = tmp_path / "fluidMechanics.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    session = _FakeSession()

    @contextlib.asynccontextmanager
    async def fake_get_async_session():
        yield session

    async def fake_load_space(sess, identifier):
        return SimpleNamespace(id=2)

    monkeypatch.setattr(tw, "get_async_session", fake_get_async_session)
    monkeypatch.setattr(storage, "_load_search_space", fake_load_space)

    record = storage.enqueue_upload_by_search_space(2, week=0, kind="textbook", pdf_path=pdf)

    assert client.ensured, "buckets must be ensured before the enqueue upload"
    [upload] = client.uploads
    assert upload["bucket"] == storage.upload_bucket
    assert upload["content_type"] == "application/pdf"
    assert upload["data"] == b"%PDF-1.4 test"
    assert upload["object_key"].startswith("search-space-2/week-00/textbook/")
    assert record.kind == "textbook"


# --------------------------------------------------------------------------- #
# SupabaseStorageClient.ensure_bucket — REST behavior
# --------------------------------------------------------------------------- #
def _client() -> SupabaseStorageClient:
    return SupabaseStorageClient(base_url="https://example.supabase.co", api_key="svc-key")


def _response(status_code: int, text: str = "") -> SimpleNamespace:
    def raise_for_status():
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")

    return SimpleNamespace(status_code=status_code, text=text, raise_for_status=raise_for_status)


def test_ensure_bucket_posts_to_bucket_endpoint(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _response(200)

    monkeypatch.setattr("vendors.supabase_storage.requests.post", fake_post)

    _client().ensure_bucket(bucket="teacher-weekly-uploads", public=False)

    [(url, kwargs)] = calls
    assert url == "https://example.supabase.co/storage/v1/bucket"
    assert kwargs["json"] == {
        "id": "teacher-weekly-uploads",
        "name": "teacher-weekly-uploads",
        "public": False,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer svc-key"


@pytest.mark.parametrize("status_code", [400, 409])
def test_ensure_bucket_tolerates_already_existing_bucket(monkeypatch, status_code):
    monkeypatch.setattr(
        "vendors.supabase_storage.requests.post",
        lambda url, **kwargs: _response(
            status_code, '{"error":"Duplicate","message":"The resource already exists"}'
        ),
    )

    _client().ensure_bucket(bucket="teacher-weekly-uploads")  # must not raise


def test_ensure_bucket_tolerates_bare_409_without_body(monkeypatch):
    """409 on bucket-create means the bucket exists, whatever the body says."""
    monkeypatch.setattr(
        "vendors.supabase_storage.requests.post",
        lambda url, **kwargs: _response(409, ""),
    )

    _client().ensure_bucket(bucket="teacher-weekly-uploads")  # must not raise


def test_ensure_bucket_raises_on_other_errors(monkeypatch):
    monkeypatch.setattr(
        "vendors.supabase_storage.requests.post",
        lambda url, **kwargs: _response(500, "internal error"),
    )

    with pytest.raises(RuntimeError):
        _client().ensure_bucket(bucket="teacher-weekly-uploads")


def test_ensure_bucket_rejects_blank_bucket_name():
    with pytest.raises(ValueError):
        _client().ensure_bucket(bucket="  ")
