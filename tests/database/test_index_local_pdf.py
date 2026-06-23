"""Pure-unit tests for scripts/index_local_pdf.py (Macro Ch.6 probe indexer).

No real DB, no real PDF, no network: ``fitz``, ``TeacherPDFIngestor``,
``create_async_engine``, ``async_sessionmaker`` and ``AITAIndexingService`` are
all patched. The tests pin the pure logic — arg/week parsing, the local-DB guard
(refusing a remote URL, accepting a local one), native Item construction,
connector-doc building, ingestor-vs-fallback selection — plus the ``index_pdf``
orchestration (happy path + not-ready failure) and ``main`` exit codes against
fully-faked infra.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.models import DocumentStatus
from indexing.connector_document import AITAConnectorDocument
import scripts.index_local_pdf as mod


# --- arg + week parsing ------------------------------------------------------


def test_parse_args_defaults():
    args = mod.parse_args(["--pdf", "ch6.pdf", "--search-space-id", "3"])
    assert args.pdf == "ch6.pdf"
    assert args.search_space_id == 3
    assert args.material_kind == "textbook"
    assert args.week == "none"


def test_parse_args_overrides():
    args = mod.parse_args(
        ["--pdf", "x.pdf", "--search-space-id", "7", "--material-kind", "slides", "--week", "2"]
    )
    assert args.material_kind == "slides"
    assert args.week == "2"


@pytest.mark.parametrize("raw", [None, "", "none", "None", "NULL", "  none  "])
def test_parse_week_none(raw):
    assert mod.parse_week(raw) is None


def test_parse_week_int():
    assert mod.parse_week("3") == 3
    assert mod.parse_week(" 12 ") == 12


# --- local-DB guard ----------------------------------------------------------


def test_is_local_db_url():
    assert mod.is_local_db_url("postgresql+asyncpg://u:p@127.0.0.1:54322/postgres")
    assert mod.is_local_db_url("postgresql+asyncpg://u:p@localhost:5432/postgres")
    assert not mod.is_local_db_url("postgresql+asyncpg://u:p@db.supabase.co:5432/postgres")


def test_guard_local_db_url_accepts_local():
    url = "postgresql+asyncpg://u:p@127.0.0.1:54322/postgres"
    assert mod.guard_local_db_url(url) == url
    assert mod.guard_local_db_url("  " + url + "  ") == url  # trimmed


def test_guard_local_db_url_rejects_empty():
    with pytest.raises(mod.LocalGuardError, match="not set"):
        mod.guard_local_db_url("")


def test_guard_local_db_url_rejects_remote():
    remote = "postgresql+asyncpg://u:p@db.uduxdniieeqbljtwocxy.supabase.co:5432/postgres"
    with pytest.raises(mod.LocalGuardError, match="does not look local"):
        mod.guard_local_db_url(remote)


# --- native fallback Item construction --------------------------------------


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text


class _FakeFitzDoc:
    """Context-manager stand-in for ``fitz.open(...)``."""

    def __init__(self, page_texts: list[str]) -> None:
        self._pages = [_FakePage(t) for t in page_texts]

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, idx: int) -> _FakePage:
        return self._pages[idx]

    def __enter__(self) -> "_FakeFitzDoc":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _patch_fitz(page_texts: list[str]):
    fake_fitz = MagicMock()
    fake_fitz.open.return_value = _FakeFitzDoc(page_texts)
    return patch.object(mod, "fitz", fake_fitz)


def test_native_items_one_per_nonempty_page():
    with _patch_fitz(["page one text", "", "page three text"]):
        items = mod._native_items(Path("ch6.pdf"), doc_id="local-pdf:abc")
    # The empty middle page is skipped; pages 1 and 3 survive.
    assert [it.page for it in items] == [1, 3]
    first = items[0]
    assert first.text == "page one text"
    assert first.raw_text == "page one text"
    assert first.type == "body"
    assert first.section_path == []
    assert first.figure_id is None
    assert first.source_pdf == "ch6.pdf"
    assert first.id == "local-pdf:abc:1:0"


def test_native_items_requires_fitz():
    with patch.object(mod, "fitz", None):
        with pytest.raises(RuntimeError, match="PyMuPDF"):
            mod._native_items(Path("ch6.pdf"), doc_id="d")


# --- extract_items: ingestor-vs-fallback selection --------------------------


def test_extract_items_prefers_ingestor():
    ingest_items = [SimpleNamespace(id="i:1:0", page=1, text="from ingestor")]
    fake_ingestor = MagicMock()
    fake_ingestor.return_value.ingest.return_value = SimpleNamespace(items=ingest_items)
    with patch.object(mod, "TeacherPDFIngestor", fake_ingestor):
        items = mod.extract_items(Path("ch6.pdf"), doc_id="d")
    assert items == ingest_items
    fake_ingestor.return_value.ingest.assert_called_once()


def test_extract_items_falls_back_when_ingestor_empty():
    fake_ingestor = MagicMock()
    fake_ingestor.return_value.ingest.return_value = SimpleNamespace(items=[])
    with patch.object(mod, "TeacherPDFIngestor", fake_ingestor), _patch_fitz(["native page"]):
        items = mod.extract_items(Path("ch6.pdf"), doc_id="d")
    assert len(items) == 1
    assert items[0].text == "native page"


def test_extract_items_falls_back_when_ingestor_raises():
    fake_ingestor = MagicMock()
    fake_ingestor.return_value.ingest.side_effect = RuntimeError("no PyMuPDF in ingestor")
    with patch.object(mod, "TeacherPDFIngestor", fake_ingestor), _patch_fitz(["native page"]):
        items = mod.extract_items(Path("ch6.pdf"), doc_id="d")
    assert len(items) == 1
    assert items[0].raw_text == "native page"


# --- connector-doc building --------------------------------------------------


def _item(text: str, page: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, raw_text=text, page=page)


def test_build_connector_doc_fields():
    items = [_item("alpha beta", 1), _item("gamma", 2)]
    doc = mod.build_connector_doc(
        pdf_path=Path("/tmp/Ch6 GDP.pdf"),
        search_space_id=3,
        material_kind="textbook",
        week=None,
        items=items,
    )
    assert isinstance(doc, AITAConnectorDocument)
    assert doc.title == "Ch6 GDP"
    assert doc.search_space_id == 3
    assert doc.material_kind == "textbook"
    assert doc.week is None
    assert doc.document_type == "EDUCATIONAL_FILE"
    assert doc.page_count == 2
    assert "alpha beta" in doc.source_markdown and "gamma" in doc.source_markdown
    assert doc.metadata["source_name"] == "Ch6 GDP.pdf"


def test_build_connector_doc_uses_raw_text_when_text_missing():
    items = [SimpleNamespace(text="", raw_text="ocr only", page=1)]
    doc = mod.build_connector_doc(
        pdf_path=Path("x.pdf"),
        search_space_id=1,
        material_kind="textbook",
        week=4,
        items=items,
    )
    assert "ocr only" in doc.source_markdown
    assert doc.week == 4


def test_build_connector_doc_empty_text_raises():
    items = [SimpleNamespace(text="", raw_text="", page=1)]
    with pytest.raises(ValueError, match="nothing to index"):
        mod.build_connector_doc(
            pdf_path=Path("x.pdf"),
            search_space_id=1,
            material_kind="textbook",
            week=None,
            items=items,
        )


def test_build_connector_doc_coerces_invalid_material_kind():
    # AITAConnectorDocument silently coerces unknown kinds to "other".
    doc = mod.build_connector_doc(
        pdf_path=Path("x.pdf"),
        search_space_id=1,
        material_kind="bogus",
        week=None,
        items=[_item("hello", 1)],
    )
    assert doc.material_kind == "other"


def test_doc_id_is_stable_and_scoped(tmp_path):
    pdf = tmp_path / "ch6.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    a = mod._doc_id_for(pdf, 3)
    b = mod._doc_id_for(pdf, 3)
    c = mod._doc_id_for(pdf, 4)
    assert a == b
    assert a != c  # different course -> different identity
    assert a.startswith("local-pdf:")


# --- index_pdf orchestration (faked engine/session/service) -----------------


class _FakeSessionCtx:
    async def __aenter__(self):
        return MagicMock(name="session")

    async def __aexit__(self, *exc):
        return False


def _fake_infra():
    """Patch the engine + sessionmaker; return the AsyncMock engine for asserts."""
    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()
    return (
        fake_engine,
        patch.object(mod, "create_async_engine", return_value=fake_engine),
        patch.object(mod, "async_sessionmaker", lambda *a, **k: (lambda: _FakeSessionCtx())),
    )


def _ready_doc(doc_id: int = 42):
    return SimpleNamespace(id=doc_id, status=DocumentStatus.ready())


def _service_returning(document) -> MagicMock:
    fake_service_cls = MagicMock()
    instance = fake_service_cls.return_value
    instance.prepare_for_indexing = AsyncMock(return_value=[SimpleNamespace(id=document.id)])
    instance.index_from_items = AsyncMock(return_value=document)
    return fake_service_cls


@pytest.mark.asyncio
async def test_index_pdf_happy_path_returns_doc_id_and_chunks():
    items = [SimpleNamespace(text="t1", raw_text="t1", page=1), SimpleNamespace(text="t2", raw_text="t2", page=1)]
    document = _ready_doc(99)
    engine, p_engine, p_factory = _fake_infra()
    with (
        patch.object(mod, "extract_items", return_value=items),
        patch.object(mod, "AITAIndexingService", _service_returning(document)),
        p_engine,
        p_factory,
    ):
        doc_id, chunk_count = await mod.index_pdf(
            db_url="postgresql+asyncpg://u:p@127.0.0.1:5432/postgres",
            pdf_path=Path("ch6.pdf"),
            search_space_id=3,
            material_kind="textbook",
            week=None,
        )
    assert doc_id == 99
    assert chunk_count == 2
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_index_pdf_raises_when_nothing_prepared():
    items = [SimpleNamespace(text="t", raw_text="t", page=1)]
    fake_service_cls = MagicMock()
    fake_service_cls.return_value.prepare_for_indexing = AsyncMock(return_value=[])
    fake_service_cls.return_value.index_from_items = AsyncMock()
    engine, p_engine, p_factory = _fake_infra()
    with (
        patch.object(mod, "extract_items", return_value=items),
        patch.object(mod, "AITAIndexingService", fake_service_cls),
        p_engine,
        p_factory,
    ):
        with pytest.raises(RuntimeError, match="no document"):
            await mod.index_pdf(
                db_url="postgresql+asyncpg://u:p@127.0.0.1:5432/postgres",
                pdf_path=Path("ch6.pdf"),
                search_space_id=3,
                material_kind="textbook",
                week=None,
            )
    engine.dispose.assert_awaited_once()  # engine still disposed in finally


@pytest.mark.asyncio
async def test_index_pdf_raises_when_not_ready():
    items = [SimpleNamespace(text="t", raw_text="t", page=1)]
    failed_doc = SimpleNamespace(id=7, status=DocumentStatus.failed("boom"))
    engine, p_engine, p_factory = _fake_infra()
    with (
        patch.object(mod, "extract_items", return_value=items),
        patch.object(mod, "AITAIndexingService", _service_returning(failed_doc)),
        p_engine,
        p_factory,
    ):
        with pytest.raises(RuntimeError, match="did not reach 'ready'"):
            await mod.index_pdf(
                db_url="postgresql+asyncpg://u:p@127.0.0.1:5432/postgres",
                pdf_path=Path("ch6.pdf"),
                search_space_id=3,
                material_kind="textbook",
                week=None,
            )
    engine.dispose.assert_awaited_once()


# --- run() / main() top-level -----------------------------------------------


@pytest.mark.asyncio
async def test_run_happy_path(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/postgres")
    args = mod.parse_args(["--pdf", "ch6.pdf", "--search-space-id", "3"])
    with (
        patch.object(Path, "is_file", return_value=True),
        patch.object(mod, "index_pdf", AsyncMock(return_value=(55, 4))),
    ):
        code = await mod.run(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "document_id=55" in out and "chunks=4" in out and "status=ready" in out


@pytest.mark.asyncio
async def test_run_rejects_remote_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql+asyncpg://u:p@db.supabase.co:5432/postgres")
    args = mod.parse_args(["--pdf", "ch6.pdf", "--search-space-id", "3"])
    with patch.object(mod, "index_pdf", AsyncMock()) as idx:
        with pytest.raises(mod.LocalGuardError):
            await mod.run(args)
    idx.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_missing_pdf_raises(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql+asyncpg://u:p@localhost:5432/postgres")
    args = mod.parse_args(["--pdf", "nope.pdf", "--search-space-id", "3"])
    with patch.object(Path, "is_file", return_value=False):
        with pytest.raises(FileNotFoundError):
            await mod.run(args)


def test_main_returns_zero_on_success():
    with patch.object(mod, "run", AsyncMock(return_value=0)):
        assert mod.main(["--pdf", "ch6.pdf", "--search-space-id", "3"]) == 0


def test_main_returns_one_on_guard_error():
    async def _raise(_args):
        raise mod.LocalGuardError("remote!")

    with patch.object(mod, "run", _raise):
        assert mod.main(["--pdf", "ch6.pdf", "--search-space-id", "3"]) == 1


def test_main_returns_one_on_runtime_error():
    async def _raise(_args):
        raise RuntimeError("index failed")

    with patch.object(mod, "run", _raise):
        assert mod.main(["--pdf", "ch6.pdf", "--search-space-id", "3"]) == 1
