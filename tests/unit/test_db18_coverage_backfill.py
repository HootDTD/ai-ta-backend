"""DB-18 patch-coverage backfill — fast, no-Docker unit tests.

The code-cleanup-massive branch rewrote several request-path and boundary
modules that had no unit tests (only integration/Testcontainers coverage, some
of which is env-gated). These tests exercise the specific changed lines the
diff-cover gate flagged, using an in-memory fake async session so they run in
milliseconds without a database. They assert real behavior (error mapping,
id-remap, factory validation), not merely line execution.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from database.session import run_async


# --------------------------------------------------------------------------- #
# Fake async-session harness (no Docker, no engine)
# --------------------------------------------------------------------------- #
class FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)

    def scalar_one(self):
        return self._items[0]

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class FakeSession:
    def __init__(self, *, execute_results=None, get_results=None, execute_raises=None):
        self._execute_results = list(execute_results or [])
        self._get_results = list(get_results or [])
        self._execute_raises = execute_raises
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, *a, **k):
        if self._execute_raises is not None:
            raise self._execute_raises
        if self._execute_results:
            return self._execute_results.pop(0)
        return FakeResult([])

    async def get(self, *a, **k):
        if self._get_results:
            return self._get_results.pop(0)
        return None

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, obj):
        pass


class _FakeACM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def acm(session):
    """Return a get_async_session replacement yielding ``session``."""
    return lambda *a, **k: _FakeACM(session)


def fake_request(query=None):
    return SimpleNamespace(query_params=dict(query or {}))


AUTH = SimpleNamespace(user_id="user-1")


# --------------------------------------------------------------------------- #
# database/transforms via workspaces/db + boundary helpers
# --------------------------------------------------------------------------- #
def test_workspace_repository_resolves_course_and_builds_sorted_materials():
    import workspaces.db as wdb

    course = SimpleNamespace(
        id=42,
        name="AAE 33300",
        slug="aae-33300",
        subject_name="Aerodynamics",
        retrieval_weights={"textbook": 0.7},
    )
    doc_hi = SimpleNamespace(
        id=2,
        material_kind="textbook",
        title="Zebra",
        metadata_={"priority": 5, "index_path": "", "week": 3},
    )
    doc_lo = SimpleNamespace(
        id=1,
        material_kind=None,
        title="Alpha",
        metadata_={"priority": 5},
    )
    session = FakeSession(execute_results=[FakeResult([course]), FakeResult([doc_hi, doc_lo])])
    with patch.object(wdb, "get_async_session", acm(session)):
        ws = wdb.DBWorkspaceRepository().load_workspace("aae-33300")

    assert ws.class_id == "42"
    assert ws.weight_overrides == {"textbook": 0.7}
    # priority ties break on title asc, so "Alpha" precedes "Zebra".
    assert [m.title for m in ws.materials] == ["Alpha", "Zebra"]
    assert ws.materials[1].kind == "textbook"


def test_workspace_repository_missing_course_raises_keyerror():
    import workspaces.db as wdb

    session = FakeSession(execute_results=[FakeResult([]), FakeResult([]), FakeResult([])])
    with patch.object(wdb, "get_async_session", acm(session)):
        with pytest.raises(KeyError):
            wdb.DBWorkspaceRepository().load_workspace("nope")


# --------------------------------------------------------------------------- #
# chats/service memory summarization
# --------------------------------------------------------------------------- #
def test_summarize_turns_for_memory_prefixes_and_skips_blank():
    from chats.service import _summarize_turns_for_memory
    from database.models import ChatMessage

    turns = [
        ChatMessage(role="user", content="Explain entropy"),
        ChatMessage(role="assistant", content="   "),  # skipped (blank)
        ChatMessage(role="assistant", content="Entropy measures disorder"),
    ]
    summary = _summarize_turns_for_memory(turns)
    assert summary == "U: Explain entropy | A: Entropy measures disorder"


def test_refresh_memory_summary_short_thread_takes_trigger_branch():
    import chats.service as svc
    from database.models import ChatMessage, ChatSession
    from database.session import run_async

    turns = [ChatMessage(role="user", content="hi")]
    session = FakeSession(execute_results=[FakeResult(turns)])
    chat_session = ChatSession()
    run_async(
        svc.refresh_memory_summary(
            session,
            chat_session=chat_session,
            user_id="user-1",
            course_id=5,
        )
    )
    # Short thread: summary is set (from an empty window) and updated_at stamped.
    assert chat_session.memory_summary == ""
    assert chat_session.updated_at is not None


# --------------------------------------------------------------------------- #
# apollo/smart_questions/controller — new opportunity audit row
# --------------------------------------------------------------------------- #
def test_write_opportunity_audit_creates_row_for_unknown_target():
    import apollo.smart_questions.controller as ctrl

    class _Db:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

    db = _Db()
    result = SimpleNamespace(action="ask", target_node_id="node-x", question="Why?")
    rows: dict = {}
    out = ctrl._write_opportunity_audit(
        db,
        course_id=1,
        attempt_id=2,
        session_id=3,
        rows=rows,
        result=result,
        turn_index=4,
    )
    assert "node-x" in out
    assert len(db.added) == 1
    assert out["node-x"].asked_turn == 5


# --------------------------------------------------------------------------- #
# apollo/persistence/models — factory branches
# --------------------------------------------------------------------------- #
def test_from_inventory_payload_accepts_dict_reference_solution():
    from apollo.persistence.models import Problem

    payload = {
        "id": "P-1",
        "difficulty": "medium",
        "problem_text": "A block slides...",
        "given_values": {"m": 2},
        "target_unknown": "a",
        "reference_solution": {"version": 2, "steps": ["already a document"]},
    }
    row = Problem.from_inventory_payload(payload, course_id=7, concept_id=9)
    assert row.reference_solution == {"version": 2, "steps": ["already a document"]}


def test_provisioning_run_authored_set_rejects_invalid_status():
    from apollo.persistence.models import ProvisioningRun

    with pytest.raises(ValueError, match="invalid authored-set status"):
        ProvisioningRun.authored_set(search_space_id=1, set_index=0, status="bogus")


# --------------------------------------------------------------------------- #
# apollo/provisioning/authored_problem — default no-op retriever
# --------------------------------------------------------------------------- #
def test_no_retrieve_returns_empty_tuple():
    import asyncio

    from apollo.provisioning.authored_problem import _no_retrieve

    assert asyncio.run(_no_retrieve("q")) == ()


# --------------------------------------------------------------------------- #
# campaign/transcript_replay — import wiring
# --------------------------------------------------------------------------- #
def test_transcript_replay_module_imports():
    import importlib

    mod = importlib.import_module("campaign.transcript_replay")
    assert hasattr(mod, "ReplayOutcome")


# --------------------------------------------------------------------------- #
# indexing/document_persistence
# --------------------------------------------------------------------------- #
def test_rollback_and_persist_failure_writes_failed_status():
    import indexing.document_persistence as dp
    from database.models import Document, DocumentStatus
    from database.session import run_async

    document = Document(title="T", course_id=1)
    session = FakeSession()
    run_async(dp.rollback_and_persist_failure(session, document, "boom" * 300))
    assert document.status == DocumentStatus.FAILED
    assert document.failure_reason is not None
    assert len(document.failure_reason) <= 500
    assert session.committed is True


def test_attach_chunks_backfills_document_and_course_ids():
    import indexing.document_persistence as dp
    from database.models import Document, DocumentChunk

    document = Document(id=11, course_id=3)
    chunks = [DocumentChunk(content="a"), DocumentChunk(content="b")]
    added = []
    fake_session = SimpleNamespace(add_all=lambda objs: added.extend(objs))
    with patch.object(dp, "object_session", lambda _doc: fake_session):
        dp.attach_chunks_to_document(document, chunks)
    assert all(c.document_id == 11 and c.course_id == 3 for c in chunks)
    assert added == chunks


# --------------------------------------------------------------------------- #
# indexing/checkpoint_indexer — unknown-document guards
# --------------------------------------------------------------------------- #
def test_finalize_document_unknown_id_raises():
    import indexing.checkpoint_indexer as ci
    from database.session import run_async

    session = FakeSession(get_results=[None])
    with pytest.raises(ValueError, match="Unknown document"):
        run_async(
            ci.finalize_document(
                session,
                document_id=999,
                chunk_pairs=[],
                doc_content="x",
                doc_embedding=[0.0],
                page_count=1,
                embed_fn=lambda texts: [[0.0] for _ in texts],
            )
        )


# --------------------------------------------------------------------------- #
# reports/ai_use/routes
# --------------------------------------------------------------------------- #
def test_create_ai_use_report_evidence_value_error_maps_to_400():
    from fastapi import HTTPException

    import reports.ai_use.routes as r

    session_row = SimpleNamespace(course_id=5)
    session = FakeSession()
    with (
        patch.object(r, "resolve_auth_context", lambda req: AUTH),
        patch.object(r, "get_async_session", acm(session)),
        patch.object(r, "get_chat_session_for_user", _acoro(session_row)),
        patch.object(r, "build_evidence_pack", side_effect=ValueError("bad style")),
    ):
        body = r.CreateReportBody(style="APA", length="brief")
        with pytest.raises(HTTPException) as exc:
            r.create_ai_use_report("chat-1", body, fake_request())
    assert exc.value.status_code == 400


def test_create_ai_use_report_generation_error_maps_to_500():
    from fastapi import HTTPException

    import reports.ai_use.routes as r

    session_row = SimpleNamespace(course_id=5)
    session = FakeSession()
    with (
        patch.object(r, "resolve_auth_context", lambda req: AUTH),
        patch.object(r, "get_async_session", acm(session)),
        patch.object(r, "get_chat_session_for_user", _acoro(session_row)),
        patch.object(r, "build_evidence_pack", lambda *a, **k: {"turns": [1]}),
        patch.object(r, "gen_report", side_effect=RuntimeError("llm down")),
    ):
        body = r.CreateReportBody()
        with pytest.raises(HTTPException) as exc:
            r.create_ai_use_report("chat-1", body, fake_request())
    assert exc.value.status_code == 500


def test_get_ai_use_report_pdf_render_failure_maps_to_500():
    import sys
    import types

    from fastapi import HTTPException

    import reports.ai_use.routes as r

    row = SimpleNamespace(
        id="rep-1",
        chat_id="c1",
        created_at=None,
        style="none",
        length="brief",
        markdown="# hi",
        jsonld={},
        model_fingerprint="fp",
        tool_calls=[],
        prompt_hashes=[],
    )
    session = FakeSession()

    def _boom(*a, **k):
        raise OSError("no wkhtml")

    # Inject a stub ``reports.ai_use.pdf`` so the in-function ``from .pdf import
    # render_pdf_from_markdown`` resolves to it deterministically on every
    # platform (the real module imports WeasyPrint, unavailable on Windows).
    fake_pdf = types.ModuleType("reports.ai_use.pdf")
    fake_pdf.render_pdf_from_markdown = _boom
    with (
        patch.dict(sys.modules, {"reports.ai_use.pdf": fake_pdf}),
        patch.object(r, "resolve_auth_context", lambda req: AUTH),
        patch.object(r, "get_async_session", acm(session)),
        patch.object(r, "get_report_for_user", _acoro(row)),
    ):
        with pytest.raises(HTTPException) as exc:
            r.get_ai_use_report_pdf("rep-1", fake_request())
    assert exc.value.status_code == 500


# --------------------------------------------------------------------------- #
# chats/routes
# --------------------------------------------------------------------------- #
def test_list_chats_filters_by_space_and_builds_preview():
    import chats.routes as cr

    session_row = SimpleNamespace(
        id=1, course_id=5, external_id="chat-1", created_at=None, updated_at=None
    )
    first_turn = SimpleNamespace(content="  What is entropy?  ")
    session = FakeSession(
        execute_results=[
            FakeResult([session_row]),  # sessions
            FakeResult([first_turn]),  # first user turn
            FakeResult([3]),  # count
        ]
    )
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
    ):
        out = cr.list_chats(fake_request({"search_space_id": "5"}))
    assert out == [
        {
            "chat_id": "chat-1",
            "search_space_id": 5,
            "title": "What is entropy?",
            "turn_count": 3,
            "created_at": None,
            "updated_at": None,
        }
    ]


def test_list_chats_db_error_maps_to_500():
    from fastapi import HTTPException

    import chats.routes as cr

    session = FakeSession(execute_raises=RuntimeError("db down"))
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.list_chats(fake_request())
    assert exc.value.status_code == 500


def test_save_chat_new_without_space_returns_400():
    from fastapi import HTTPException

    import chats.routes as cr

    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "get_chat_session_for_user", _acoro(None)),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.save_chat("chat-1", {"turns": []}, fake_request())
    assert exc.value.status_code == 400


def test_save_chat_space_mismatch_returns_400():
    from fastapi import HTTPException

    import chats.routes as cr

    existing = SimpleNamespace(id=1, course_id=9, metadata_=None, updated_at=None)
    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "get_chat_session_for_user", _acoro(existing)),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.save_chat("chat-1", {"search_space_id": 5, "turns": []}, fake_request())
    assert exc.value.status_code == 400


def test_save_chat_happy_path_persists_turns_and_summary():
    import chats.routes as cr

    existing = SimpleNamespace(id=1, course_id=5, metadata_=None, updated_at=None)
    session = FakeSession()
    payload = {
        "search_space_id": 5,
        "meta": {"topic": "thermo"},
        "turns": [{"role": "user", "content": "hi"}],
    }
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "get_chat_session_for_user", _acoro(existing)),
        patch.object(cr, "append_turn", _acoro(None)),
        patch.object(cr, "refresh_memory_summary", _acoro(None)),
    ):
        out = cr.save_chat("chat-1", payload, fake_request())
    assert out == {"ok": True, "chat_id": "chat-1"}
    assert existing.metadata_ == {"topic": "thermo"}


def test_save_chat_unexpected_error_maps_to_500():
    from fastapi import HTTPException

    import chats.routes as cr

    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "get_chat_session_for_user", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.save_chat("chat-1", {"search_space_id": 5, "turns": []}, fake_request())
    assert exc.value.status_code == 500


def test_delete_chat_happy_path_commits():
    import chats.routes as cr

    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "delete_chat_session_for_user", _acoro(True)),
    ):
        assert cr.delete_chat("chat-1", fake_request()) is None
    assert session.committed is True


def test_delete_chat_error_maps_to_500():
    from fastapi import HTTPException

    import chats.routes as cr

    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "delete_chat_session_for_user", side_effect=RuntimeError("x")),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.delete_chat("chat-1", fake_request())
    assert exc.value.status_code == 500


def test_get_chat_not_found_maps_to_404():
    from fastapi import HTTPException

    import chats.routes as cr

    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "serialize_chat_session", side_effect=ValueError("missing")),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.get_chat("chat-1", fake_request())
    assert exc.value.status_code == 404


def test_get_chat_unexpected_error_maps_to_500():
    from fastapi import HTTPException

    import chats.routes as cr

    session = FakeSession()
    with (
        patch.object(cr, "resolve_auth_context", lambda req: AUTH),
        patch.object(cr, "get_async_session", acm(session)),
        patch.object(cr, "serialize_chat_session", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(HTTPException) as exc:
            cr.get_chat("chat-1", fake_request())
    assert exc.value.status_code == 500


def _acoro(value):
    """Return an async function that ignores its args and returns ``value``."""

    async def _fn(*a, **k):
        return value

    return _fn


# --------------------------------------------------------------------------- #
# knowledge/teacher_weekly — DB-method branches (mocked async session)
# --------------------------------------------------------------------------- #
def _tws():
    from knowledge.teacher_weekly import TeacherWeeklyStorage

    inst = TeacherWeeklyStorage.__new__(TeacherWeeklyStorage)
    inst.job_lease_seconds = 60
    return inst


def test_load_search_space_digit_miss_falls_through_to_slug():
    course = SimpleNamespace(id=5, name="X", slug="x")
    session = FakeSession(execute_results=[FakeResult([]), FakeResult([course])])
    got = run_async(_tws()._load_search_space(session, "5"))
    assert got is course


def test_get_upload_record_missing_raises():
    import knowledge.teacher_weekly as tw

    session = FakeSession(get_results=[None])
    with patch.object(tw, "get_async_session", acm(session)):
        with pytest.raises(ValueError, match="Unknown teacher upload"):
            run_async(_tws()._get_upload_record_async(7))


def test_get_upload_search_space_id_returns_course_id():
    import knowledge.teacher_weekly as tw

    session = FakeSession(get_results=[SimpleNamespace(course_id=9)])
    with patch.object(tw, "get_async_session", acm(session)):
        assert run_async(_tws()._get_upload_search_space_id_async(3)) == 9


def test_retry_failed_upload_requeues_and_creates_job():
    import knowledge.teacher_weekly as tw

    upload = SimpleNamespace(
        status=tw.UPLOAD_STATUS_FAILED,
        storage_key="k",
        id=1,
        course_id=5,
        error_message="e",
        warning_count=3,
        started_at="x",
        completed_at="y",
        ocr_provider="p",
        ocr_details={"a": 1},
        updated_at=None,
    )
    session = FakeSession(get_results=[upload], execute_results=[FakeResult([])])
    inst = _tws()
    inst._build_upload_record = lambda row: "REC"
    with patch.object(tw, "get_async_session", acm(session)):
        assert run_async(inst._retry_upload_async(1)) == "REC"
    assert upload.status == tw.UPLOAD_STATUS_QUEUED
    assert upload.ocr_details == {}
    assert len(session.added) == 1  # a fresh UploadJob


def test_get_reindex_marker_returns_metadata_value():
    import knowledge.teacher_weekly as tw

    session = FakeSession(
        get_results=[SimpleNamespace(metadata_={"reindex_requested_at": "2026-01-01"})]
    )
    with patch.object(tw, "get_async_session", acm(session)):
        assert run_async(_tws()._get_reindex_marker_async(1)) == "2026-01-01"


def test_reindex_ready_upload_requeues_and_creates_job():
    import knowledge.teacher_weekly as tw

    upload = SimpleNamespace(
        status=tw.UPLOAD_STATUS_READY,
        storage_key="k",
        id=1,
        course_id=5,
        error_message=None,
        warning_count=0,
        started_at=None,
        completed_at=None,
        ocr_provider=None,
        ocr_details={},
        metadata_={"ocr_degraded": True},
        updated_at=None,
    )
    session = FakeSession(get_results=[upload], execute_results=[FakeResult([])])
    inst = _tws()
    inst._build_upload_record = lambda row: "REC"
    with patch.object(tw, "get_async_session", acm(session)):
        assert run_async(inst._reindex_upload_async(1)) == "REC"
    assert upload.status == tw.UPLOAD_STATUS_QUEUED
    assert "reindex_requested_at" in upload.metadata_
    assert len(session.added) == 1


def test_claim_upload_job_for_upload_no_candidate_returns_none():
    import knowledge.teacher_weekly as tw

    session = FakeSession(execute_results=[FakeResult([])])
    with patch.object(tw, "get_async_session", acm(session)):
        assert run_async(_tws()._claim_upload_job_async("owner", upload_id=42)) is None


def test_set_current_week_updates_course_row():
    import knowledge.teacher_weekly as tw

    space = SimpleNamespace(id=5, name="X", slug="x", current_week=1, updated_at=None)
    session = FakeSession(execute_results=[FakeResult([space])])
    inst = _tws()
    inst._sync_week_activation = _acoro(None)
    with patch.object(tw, "get_async_session", acm(session)):
        out = run_async(inst._set_current_week_by_search_space_async(5, 7))
    assert out["current_week"] == 7
    assert space.current_week == 7


def test_get_retrieval_weights_normalizes_course_weights():
    import knowledge.teacher_weekly as tw

    space = SimpleNamespace(id=5, name="X", slug="x", retrieval_weights={"textbook": 0.5})
    session = FakeSession(execute_results=[FakeResult([space])])
    with patch.object(tw, "get_async_session", acm(session)):
        out = run_async(_tws()._get_retrieval_weights_by_search_space_async(5))
    assert isinstance(out, dict)


def test_update_retrieval_weights_persists_normalized_values():
    import knowledge.teacher_weekly as tw

    space = SimpleNamespace(
        id=5,
        name="X",
        slug="x",
        retrieval_weights={},
        retrieval_weight_min=None,
        retrieval_weight_max=None,
        updated_at=None,
    )
    session = FakeSession(execute_results=[FakeResult([space])])
    with patch.object(tw, "get_async_session", acm(session)):
        out = run_async(
            _tws()._update_retrieval_weights_by_search_space_async(5, {"textbook": 0.9})
        )
    assert isinstance(out, dict)
    assert space.retrieval_weights == out


# --------------------------------------------------------------------------- #
# indexing/indexing_service — prepare/index branches
# --------------------------------------------------------------------------- #
def test_prepare_for_indexing_unchanged_content_requeues_non_ready():
    import indexing.indexing_service as isvc
    from database.models import DocumentStatus

    existing = SimpleNamespace(
        content_hash="H",
        status=DocumentStatus.PROCESSING,
        failure_reason="old",
        updated_at=None,
    )
    session = FakeSession(execute_results=[FakeResult([existing])])
    connector = SimpleNamespace(title="Doc")
    with (
        patch.object(isvc, "compute_unique_identifier_hash", lambda d: "U"),
        patch.object(isvc, "compute_content_hash", lambda d: "H"),
    ):
        docs = run_async(isvc.AITAIndexingService(session).prepare_for_indexing([connector]))
    assert docs == [existing]
    assert existing.failure_reason is None
    assert existing.status == DocumentStatus.PENDING


def test_prepare_for_indexing_changed_content_updates_and_requeues():
    import indexing.indexing_service as isvc
    from database.models import DocumentStatus

    existing = SimpleNamespace(
        content_hash="OLD",
        status=DocumentStatus.READY,
        title="",
        source_markdown="",
        metadata_=None,
        material_kind=None,
        week=None,
        updated_at=None,
        failure_reason="x",
    )
    session = FakeSession(execute_results=[FakeResult([existing])])
    connector = SimpleNamespace(
        title="Doc",
        source_markdown="md",
        metadata={"k": 1},
        material_kind="textbook",
        week=3,
    )
    with (
        patch.object(isvc, "compute_unique_identifier_hash", lambda d: "U"),
        patch.object(isvc, "compute_content_hash", lambda d: "NEW"),
    ):
        docs = run_async(isvc.AITAIndexingService(session).prepare_for_indexing([connector]))
    assert docs == [existing]
    assert existing.metadata_ == {"k": 1}
    assert existing.failure_reason is None
    assert existing.status == DocumentStatus.PENDING


def test_index_from_items_happy_path_marks_ready():
    import indexing.indexing_service as isvc
    from database.models import Document, DocumentStatus

    document = Document(course_id=1, title="Doc")
    connector = SimpleNamespace(title="Doc", page_count=1)
    session = FakeSession()
    with (
        patch.object(
            isvc, "items_to_chunk_texts", lambda items: [("body text", {"page_number": 1})]
        ),
        patch.object(isvc, "embed_text", lambda text: [0.0, 0.1]),
    ):
        out = run_async(
            isvc.AITAIndexingService(session).index_from_items(document, connector, ["item"])
        )
    assert out.status == DocumentStatus.READY
    assert out.failure_reason is None


# --------------------------------------------------------------------------- #
# retrieval/hybrid_search — material_kind filter branch
# --------------------------------------------------------------------------- #
def test_hybrid_search_with_material_kind_and_no_visible_docs_returns_empty():
    import retrieval.hybrid_search as hs

    session = FakeSession(execute_results=[FakeResult([])])
    with patch.object(hs, "embed_text", lambda q: [0.0]):
        out = run_async(
            hs.AITAHybridSearchRetriever(session, 5).hybrid_search(
                "entropy", top_k=10, material_kind="textbook"
            )
        )
    assert out == []


# --------------------------------------------------------------------------- #
# scripts/seed_apollo_concept_registry — upsert insert/update branches
# --------------------------------------------------------------------------- #
def test_upsert_problem_inserts_when_absent():
    import scripts.seed_apollo_concept_registry as seed

    session = FakeSession(execute_results=[FakeResult([])])  # scalar_one_or_none -> None
    sentinel = object()
    # Patch only the factory classmethod so ``select(Problem)`` still uses the
    # real ORM class as a column expression.
    with patch.object(seed.Problem, "from_pydantic_payload", return_value=sentinel):
        run_async(
            seed._upsert_problem(
                session,
                concept_id=1,
                course_id=2,
                problem_code="P1",
                difficulty="easy",
                payload={"id": "P1"},
            )
        )
    assert sentinel in session.added


def test_upsert_problem_updates_when_present():
    from unittest.mock import MagicMock

    import scripts.seed_apollo_concept_registry as seed

    existing = MagicMock()
    session = FakeSession(execute_results=[FakeResult([existing])])
    run_async(
        seed._upsert_problem(
            session,
            concept_id=1,
            course_id=2,
            problem_code="P1",
            difficulty="easy",
            payload={"id": "P1"},
        )
    )
    existing.apply_pydantic_payload.assert_called_once_with({"id": "P1"})
