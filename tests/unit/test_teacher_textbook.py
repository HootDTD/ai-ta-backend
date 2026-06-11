"""Unit tests for course-wide textbook support in TeacherWeeklyStorage.

Pure-logic coverage (no DB, no Docker): week normalization, document-week
resolution, upload-input validation, and course-payload assembly. The single
DB-glue line in ``_list_course_by_search_space_async`` is covered by faking the
async session seam.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

import knowledge.teacher_weekly as tw
from knowledge.teacher_weekly import (
    COURSE_WIDE_KINDS,
    COURSE_WIDE_WEEK,
    VALID_KINDS,
    WEEKLY_KINDS,
    TeacherWeeklyStorage,
    _document_week,
    _normalize_upload_week,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _make_storage(tmp_path, total_weeks: int = 3) -> TeacherWeeklyStorage:
    """Construct storage without touching Supabase (inject a dummy client)."""
    return TeacherWeeklyStorage(
        base_dir=tmp_path,
        total_weeks=total_weeks,
        storage_client=object(),
    )


def _fake_upload_row(**overrides):
    """A lightweight stand-in for a TeacherUpload ORM row, carrying every
    attribute ``_build_upload_record`` reads."""
    defaults = dict(
        id=1,
        week=1,
        kind="notes",
        title="Week 1 Notes",
        status="ready",
        uploaded_at=None,
        source_name="wk1.pdf",
        page_count=5,
        doc_id="10",
        metadata_={},
        error_message=None,
        warning_count=0,
        started_at=None,
        completed_at=None,
        ocr_provider=None,
        ocr_summary={},
        is_latest=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# --------------------------------------------------------------------------- #
# Kind constants
# --------------------------------------------------------------------------- #
def test_textbook_is_a_valid_course_wide_kind():
    assert "textbook" in COURSE_WIDE_KINDS
    assert "textbook" in VALID_KINDS
    assert "textbook" not in WEEKLY_KINDS
    assert WEEKLY_KINDS == {"notes", "slides"}


# --------------------------------------------------------------------------- #
# _normalize_upload_week
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("submitted_week", [0, 1, 7, 99, None])
def test_normalize_week_textbook_always_sentinel(submitted_week):
    # Whatever week the client sends, a textbook collapses to the sentinel.
    assert _normalize_upload_week("textbook", submitted_week, total_weeks=16) == COURSE_WIDE_WEEK


def test_normalize_week_textbook_case_insensitive():
    assert _normalize_upload_week("  TextBook ", 4, total_weeks=16) == COURSE_WIDE_WEEK


@pytest.mark.parametrize("week", [1, 8, 16])
def test_normalize_week_weekly_in_range(week):
    assert _normalize_upload_week("notes", week, total_weeks=16) == week


@pytest.mark.parametrize("week", [0, -1, 17])
def test_normalize_week_weekly_out_of_range_raises(week):
    with pytest.raises(ValueError, match="between 1 and 16"):
        _normalize_upload_week("slides", week, total_weeks=16)


def test_normalize_week_unknown_kind_raises_with_textbook_in_message():
    with pytest.raises(ValueError, match="textbook"):
        _normalize_upload_week("video", 1, total_weeks=16)


# --------------------------------------------------------------------------- #
# _document_week
# --------------------------------------------------------------------------- #
def test_document_week_textbook_is_null():
    # Course-wide doc stores NULL → stays visible across every week.
    assert _document_week("textbook", COURSE_WIDE_WEEK) is None


@pytest.mark.parametrize("kind", ["notes", "slides"])
def test_document_week_weekly_passthrough(kind):
    assert _document_week(kind, 5) == 5


# --------------------------------------------------------------------------- #
# _validate_upload_input
# --------------------------------------------------------------------------- #
def test_validate_textbook_forces_sentinel_and_titles(tmp_path):
    storage = _make_storage(tmp_path)
    pdf = tmp_path / "fluids.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    week_val, kind_norm, resolved_pdf, title = storage._validate_upload_input(
        week=9,  # ignored for textbook
        kind="textbook",
        pdf_path=pdf,
        title="",
    )
    assert week_val == COURSE_WIDE_WEEK
    assert kind_norm == "textbook"
    assert resolved_pdf == pdf.resolve()
    assert title == "Textbook"  # week-free default for course-wide material


def test_validate_textbook_keeps_explicit_title(tmp_path):
    storage = _make_storage(tmp_path)
    pdf = tmp_path / "fluids.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    _, _, _, title = storage._validate_upload_input(
        week=1, kind="textbook", pdf_path=pdf, title="White, Fluid Mechanics 8e"
    )
    assert title == "White, Fluid Mechanics 8e"


def test_validate_weekly_default_title(tmp_path):
    storage = _make_storage(tmp_path, total_weeks=4)
    pdf = tmp_path / "wk.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    week_val, kind_norm, _, title = storage._validate_upload_input(
        week=2, kind="notes", pdf_path=pdf, title=""
    )
    assert (week_val, kind_norm) == (2, "notes")
    assert title == "Week 2 Notes"  # weekly default carries the week number


def test_validate_weekly_enforces_range(tmp_path):
    storage = _make_storage(tmp_path, total_weeks=3)
    pdf = tmp_path / "wk.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    with pytest.raises(ValueError, match="between 1 and 3"):
        storage._validate_upload_input(week=4, kind="notes", pdf_path=pdf, title="")


def test_validate_missing_file_raises(tmp_path):
    storage = _make_storage(tmp_path)
    with pytest.raises(FileNotFoundError):
        storage._validate_upload_input(
            week=1, kind="textbook", pdf_path=tmp_path / "nope.pdf", title=""
        )


# --------------------------------------------------------------------------- #
# _ingest_pdf_upload — document week resolution
# --------------------------------------------------------------------------- #
class _FakeIngestion:
    items = [SimpleNamespace()]
    source_markdown = "body text"
    page_count = 3
    pages: list = []
    artifact_manifest: dict = {}
    ocr_provider = "native"
    ocr_summary: dict = {}
    warning_count = 0
    warnings: list = []


class _FakeIngestor:
    def __init__(self, *a, **k):
        pass

    def ingest(self, pdf_path, *, doc_id, upload_page_asset):
        return _FakeIngestion()


def _claimed(tmp_path, *, kind, week):
    pdf = tmp_path / "src.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    claimed = tw.ClaimedUploadJob(
        job_id=1, upload_id=2, search_space_id=2, week=week, kind=kind,
        title="Fluids Textbook" if kind == "textbook" else "Week Notes",
        source_name="src.pdf", storage_key="k",
    )
    return claimed, pdf


def test_ingest_textbook_indexes_with_null_week(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "TeacherPDFIngestor", _FakeIngestor)
    storage = _make_storage(tmp_path)
    claimed, pdf = _claimed(tmp_path, kind="textbook", week=COURSE_WIDE_WEEK)

    connector_doc, _ingestion, _sha = storage._ingest_pdf_upload(claimed=claimed, pdf_path=pdf)

    assert connector_doc.material_kind == "textbook"
    assert connector_doc.week is None  # matches prod's course-wide textbook
    assert connector_doc.metadata["week"] is None


def test_ingest_weekly_keeps_week(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "TeacherPDFIngestor", _FakeIngestor)
    storage = _make_storage(tmp_path)
    claimed, pdf = _claimed(tmp_path, kind="notes", week=3)

    connector_doc, _ingestion, _sha = storage._ingest_pdf_upload(claimed=claimed, pdf_path=pdf)

    assert connector_doc.material_kind == "notes"
    assert connector_doc.week == 3


# --------------------------------------------------------------------------- #
# _assemble_course_payload
# --------------------------------------------------------------------------- #
def test_assemble_payload_splits_weekly_grid_and_textbook(tmp_path):
    storage = _make_storage(tmp_path, total_weeks=3)
    uploads = [
        _fake_upload_row(id=1, week=1, kind="notes", title="W1 Notes"),
        _fake_upload_row(id=2, week=2, kind="slides", title="W2 Slides"),
        _fake_upload_row(id=3, week=COURSE_WIDE_WEEK, kind="textbook", title="Fluids Textbook"),
    ]

    payload = storage._assemble_course_payload(
        search_space_id=2,
        course="E2E Fluids",
        slug="e2e-fluids",
        current_week=2,
        uploads=uploads,
    )

    assert payload["search_space_id"] == 2
    assert payload["course"] == "E2E Fluids"
    assert len(payload["weeks"]) == 3  # total_weeks

    # Weekly grid is correctly populated by week...
    wk1, wk2, wk3 = payload["weeks"]
    assert wk1["notes"]["latest"]["title"] == "W1 Notes"
    assert wk2["slides"]["latest"]["title"] == "W2 Slides"
    assert wk3["notes"]["latest"] is None and wk3["slides"]["latest"] is None

    # ...and the textbook NEVER leaks into the weekly grid (its week=0).
    for wk in payload["weeks"]:
        assert wk["notes"]["latest"] is None or wk["notes"]["latest"]["kind"] == "notes"
        assert wk["slides"]["latest"] is None or wk["slides"]["latest"]["kind"] == "slides"

    # Course-level textbook section is present.
    assert payload["textbook"]["latest"]["title"] == "Fluids Textbook"
    assert payload["textbook"]["latest"]["kind"] == "textbook"
    assert len(payload["textbook"]["history"]) == 1


def test_assemble_payload_empty_textbook_when_none_uploaded(tmp_path):
    storage = _make_storage(tmp_path, total_weeks=2)
    payload = storage._assemble_course_payload(
        search_space_id=2, course="E2E Fluids", slug="e2e-fluids",
        current_week=1, uploads=[_fake_upload_row(week=1, kind="notes")],
    )
    assert payload["textbook"] == {"latest": None, "history": []}


def test_assemble_payload_textbook_replace_keeps_history(tmp_path):
    storage = _make_storage(tmp_path, total_weeks=2)
    uploads = [
        _fake_upload_row(id=5, week=COURSE_WIDE_WEEK, kind="textbook",
                         title="New Textbook", is_latest=True),
        _fake_upload_row(id=4, week=COURSE_WIDE_WEEK, kind="textbook",
                         title="Old Textbook", is_latest=False, status="superseded"),
    ]
    payload = storage._assemble_course_payload(
        search_space_id=2, course="E2E Fluids", slug="e2e-fluids",
        current_week=1, uploads=uploads,
    )
    assert payload["textbook"]["latest"]["title"] == "New Textbook"
    assert len(payload["textbook"]["history"]) == 2


# --------------------------------------------------------------------------- #
# DB-glue: list_course_by_search_space → _assemble_course_payload
# --------------------------------------------------------------------------- #
def test_list_course_returns_textbook_section(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path, total_weeks=2)
    rows = [
        _fake_upload_row(id=1, week=1, kind="notes"),
        _fake_upload_row(id=2, week=COURSE_WIDE_WEEK, kind="textbook", title="Fluids Textbook"),
    ]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return rows

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def _fake_get_async_session():
        yield _Session()

    async def _fake_load_space(_session, _ssid):
        return SimpleNamespace(name="E2E Fluids", slug="e2e-fluids")

    async def _fake_get_course(_session, *, search_space_id):
        return SimpleNamespace(current_week=1)

    monkeypatch.setattr(tw, "get_async_session", _fake_get_async_session)
    monkeypatch.setattr(storage, "_load_search_space", _fake_load_space)
    monkeypatch.setattr(storage, "_get_or_create_teacher_course", _fake_get_course)

    payload = storage.list_course_by_search_space(2)

    assert payload["course"] == "E2E Fluids"
    assert payload["textbook"]["latest"]["title"] == "Fluids Textbook"
    assert len(payload["weeks"]) == 2
