import importlib
import io
import os
import re
import uuid
import base64
import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional, Sequence, Iterator, Iterable, Union, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json as _json
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout

from .ai.vision import vision_transcribe
from .config.settings import get_runtime_dir, set_subject_name, RequestConfig
from .ai.orchestrator import Orchestrator
from .knowledge.manager import KnowledgeManager
from .config.weights import WEIGHT_MIN, WEIGHT_MAX, get_env_weights
from .ai.main_ai import extract_keywords, parse_question, solve_with_bundle, format_answer
from .config.contracts import ParsedTask
from .citations.formatter import format_citations
from .knowledge.teacher_weekly import TeacherWeeklyStorage
from .workspaces.manager import (
    WorkspaceConfigError,
    WorkspaceNotFound,
    WorkspaceError,
    build_workspace_manager,
)
from .auth import (
    AuthContext,
    auto_enroll_student_membership,
    has_membership,
    resolve_auth_context,
    validate_required_env,
)
from .database.session import get_async_session, run_async
from .chats.service import (
    append_turn,
    build_memory_context,
    get_chat_session_for_user,
    get_or_create_chat_session_for_user,
    list_recent_turns,
    refresh_memory_summary,
)

try:  # pragma: no cover - optional dependency detection
    importlib.import_module("python_multipart")
    _HAS_MULTIPART = True
except ModuleNotFoundError:  # pragma: no cover
    _HAS_MULTIPART = False

# Load environment variables from .env if present (python-dotenv preferred),
# with a minimal fallback parser so local dev works without extra deps.
try:  # pragma: no cover - convenience
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    def _load_env_fallback() -> None:
        try:
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            env_path = os.path.join(repo_root, ".env")
            if not os.path.isfile(env_path):
                return
            with open(env_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if "=" not in s:
                        continue
                    key, val = s.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass

    _load_env_fallback()

log = logging.getLogger("ai_ta_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# -------- Models --------
class AttachmentIn(BaseModel):
    name: str
    mime: str = Field(..., description="e.g., image/png")
    data_url: str = Field(..., description="data:<mime>;base64,<...>")


_teacher_storage: Optional[TeacherWeeklyStorage] = None
try:
    TEACHER_TOTAL_WEEKS = int(os.getenv("TEACHER_TOTAL_WEEKS", "16"))
except ValueError:
    TEACHER_TOTAL_WEEKS = 16


def _get_teacher_storage() -> TeacherWeeklyStorage:
    global _teacher_storage
    if _teacher_storage is None:
        base_dir = os.getenv("TEACHER_WEEKS_DIR")
        _teacher_storage = TeacherWeeklyStorage(base_dir=base_dir, total_weeks=TEACHER_TOTAL_WEEKS)
    return _teacher_storage


_workspace_manager = None


def _get_workspace_manager():
    """Return the workspace manager, creating it lazily on first call."""
    global _workspace_manager
    if _workspace_manager is None:
        try:
            _workspace_manager = build_workspace_manager()
        except WorkspaceConfigError as exc:
            log.error("Failed to initialize workspace manager: %s", exc)
            raise
    return _workspace_manager


class AskRequest(BaseModel):
    question: str
    chat_id: str = Field(..., min_length=1, max_length=200)
    search_space_id: int = Field(..., gt=0)
    class_name: Optional[str] = Field(
        default=None,
        alias="class",
        description="Deprecated: class identifier is ignored by ask endpoints; use search_space_id.",
    )
    course_id: Optional[str] = None
    doc_sets: Optional[List[str]] = Field(
        default=None,
        description="Deprecated: doc_sets overrides are ignored; configure materials via class workspace.",
    )
    attachments: Optional[List[AttachmentIn]] = Field(default_factory=list)
    alias_miner: Optional[bool] = None
    proximity: Optional[bool] = None
    prf: Optional[bool] = None
    def_bias: Optional[bool] = None
    sanitize: Optional[bool] = None

    class Config:
        allow_population_by_field_name = True


class KnowledgeMaterialOut(BaseModel):
    id: str
    subject: str
    title: str
    doc_id: str
    index_dir: str
    index_path: str
    created_at: str
    model: Optional[str] = None
    dimensions: Optional[int] = None
    page_count: Optional[int] = None


class KnowledgeSubjectOut(BaseModel):
    subject: str
    slug: str
    materials: List[KnowledgeMaterialOut]


class TeacherUploadOut(BaseModel):
    id: str
    week: int
    kind: str
    title: str
    uploaded_at: Optional[str] = None
    source_name: Optional[str] = None
    page_count: Optional[int] = None
    index_path: Optional[str] = None
    doc_id: Optional[str] = None
    material_id: Optional[str] = None


class TeacherSectionOut(BaseModel):
    latest: Optional[TeacherUploadOut]
    history: List[TeacherUploadOut]


class TeacherWeekOut(BaseModel):
    week: int
    notes: TeacherSectionOut
    slides: TeacherSectionOut


class WeightBoundsOut(BaseModel):
    min: float = Field(..., ge=0.0)
    max: float = Field(..., gt=0.0)


class RetrievalWeightValues(BaseModel):
    textbook: float = Field(..., ge=WEIGHT_MIN, le=WEIGHT_MAX)
    slides: float = Field(..., ge=WEIGHT_MIN, le=WEIGHT_MAX)
    notes: float = Field(..., ge=WEIGHT_MIN, le=WEIGHT_MAX)
    homework: float = Field(..., ge=WEIGHT_MIN, le=WEIGHT_MAX)
    exams: float = Field(..., ge=WEIGHT_MIN, le=WEIGHT_MAX)
    other: float = Field(..., ge=WEIGHT_MIN, le=WEIGHT_MAX)

    def to_dict(self) -> Dict[str, float]:
        return {
            "textbook": self.textbook,
            "slides": self.slides,
            "notes": self.notes,
            "homework": self.homework,
            "exams": self.exams,
            "other": self.other,
        }


class TeacherRetrievalWeightsOut(BaseModel):
    search_space_id: int
    course: str
    weights: RetrievalWeightValues
    defaults: RetrievalWeightValues
    bounds: WeightBoundsOut


class TeacherRetrievalWeightsUpdateIn(BaseModel):
    search_space_id: int = Field(..., gt=0)
    weights: RetrievalWeightValues


class TeacherCourseOut(BaseModel):
    search_space_id: int
    course: str
    slug: str
    current_week: int
    weeks: List[TeacherWeekOut]


class TeacherCurrentWeekIn(BaseModel):
    search_space_id: int = Field(..., gt=0)
    current_week: int = Field(..., ge=1, le=TEACHER_TOTAL_WEEKS)

    class Config:
        allow_population_by_field_name = True




# -------- Utils --------
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)


def _serialize_upload(entry: Optional[Dict[str, Any]]) -> Optional[TeacherUploadOut]:
    if not isinstance(entry, dict):
        return None
    try:
        return TeacherUploadOut(**entry)
    except Exception:
        log.warning("Failed to serialize teacher upload entry")
        return None


def _serialize_section(section: Optional[Dict[str, Any]]) -> TeacherSectionOut:
    latest = _serialize_upload((section or {}).get("latest"))
    history_raw = (section or {}).get("history") or []
    history: List[TeacherUploadOut] = []
    for item in history_raw:
        serialized = _serialize_upload(item)
        if serialized:
            history.append(serialized)
    return TeacherSectionOut(latest=latest, history=history)


def _serialize_course_payload(payload: Dict[str, Any]) -> TeacherCourseOut:
    weeks_out: List[TeacherWeekOut] = []
    for block in payload.get("weeks", []):
        if not isinstance(block, dict):
            continue
        week_num = int(block.get("week", 0) or 0)
        notes = _serialize_section(block.get("notes"))
        slides = _serialize_section(block.get("slides"))
        weeks_out.append(TeacherWeekOut(week=week_num, notes=notes, slides=slides))
    weeks_out.sort(key=lambda w: w.week)
    return TeacherCourseOut(
        search_space_id=int(payload.get("search_space_id", 0) or 0),
        course=str(payload.get("course", "")),
        slug=str(payload.get("slug", "")),
        current_week=int(payload.get("current_week", 1) or 1),
        weeks=weeks_out,
    )


def _resolve_request_auth(request: Request) -> AuthContext:
    try:
        return resolve_auth_context(request)
    except HTTPException:
        raise
    except RuntimeError as exc:
        log.error("Auth config validation failed: %s", exc)
        raise HTTPException(status_code=500, detail="Server auth configuration error")


def _require_course_membership(
    request: Request,
    *,
    search_space_id: int,
    role: Optional[str] = None,
) -> AuthContext:
    auth = _resolve_request_auth(request)

    async def _run() -> bool:
        async with get_async_session() as db_session:
            allowed = await has_membership(
                db_session,
                user_id=auth.user_id,
                search_space_id=search_space_id,
                role=role,
            )
            if allowed:
                return True

            # Optional convenience path: enroll authenticated users as students
            # on first access to student endpoints (role unset or student).
            if role in (None, "student"):
                enrolled = await auto_enroll_student_membership(
                    db_session,
                    user_id=auth.user_id,
                    search_space_id=search_space_id,
                )
                if enrolled:
                    return await has_membership(
                        db_session,
                        user_id=auth.user_id,
                        search_space_id=search_space_id,
                        role=role,
                    )
            return False

    try:
        allowed = run_async(_run())
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Membership check failed for user=%s space=%s: %s", auth.user_id, search_space_id, exc)
        raise HTTPException(status_code=500, detail="Membership validation failed")

    if not allowed:
        raise HTTPException(status_code=403, detail="Forbidden for this course")
    return auth


def _attachments_for_chat_turn(attachments: Sequence[AttachmentIn]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for att in attachments:
        out.append(
            {
                "name": att.name,
                "mime": att.mime,
            }
        )
    return out


def _user_turn_content(question: str, attachments: Sequence[AttachmentIn]) -> str:
    cleaned = (question or "").strip()
    if cleaned:
        return cleaned
    if attachments:
        return "[image-only question]"
    return ""


async def _load_memory_and_append_user_turn_async(
    *,
    auth: AuthContext,
    chat_id: str,
    search_space_id: int,
    user_content: str,
    attachments: Sequence[AttachmentIn],
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    async with get_async_session() as db_session:
        existing = await get_chat_session_for_user(
            db_session,
            chat_id=chat_id,
            user_id=auth.user_id,
        )
        if existing is not None and int(existing.search_space_id) != int(search_space_id):
            raise HTTPException(status_code=400, detail="search_space_id mismatch for chat_id")

        session = existing or await get_or_create_chat_session_for_user(
            db_session,
            chat_id=chat_id,
            user_id=auth.user_id,
            search_space_id=int(search_space_id),
            meta=meta or {},
        )
        if meta:
            session.meta = meta

        recent_turns = await list_recent_turns(
            db_session,
            chat_session_id=int(session.id),
        )
        memory_context = build_memory_context(session.memory_summary or "", recent_turns)

        await append_turn(
            db_session,
            chat_session_id=int(session.id),
            role="user",
            content=user_content,
            attachments=_attachments_for_chat_turn(attachments),
        )
        session.updated_at = datetime.now(UTC)
        await db_session.commit()
        return memory_context


def _load_memory_and_append_user_turn(
    *,
    auth: AuthContext,
    chat_id: str,
    search_space_id: int,
    user_content: str,
    attachments: Sequence[AttachmentIn],
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    return run_async(
        _load_memory_and_append_user_turn_async(
            auth=auth,
            chat_id=chat_id,
            search_space_id=search_space_id,
            user_content=user_content,
            attachments=attachments,
            meta=meta,
        )
    )


def _debug_error_detail(prefix: str, exc: Exception) -> str:
    """Attach exception detail only when explicitly enabled for local debugging."""
    if (os.getenv("DEBUG_HTTP_ERRORS", "0") or "").strip() != "1":
        return prefix
    return f"{prefix}: {type(exc).__name__}: {exc}"


async def _append_assistant_turn_and_refresh_async(
    *,
    auth: AuthContext,
    chat_id: str,
    search_space_id: int,
    assistant_content: str,
) -> None:
    async with get_async_session() as db_session:
        session = await get_chat_session_for_user(
            db_session,
            chat_id=chat_id,
            user_id=auth.user_id,
        )
        if session is None:
            # Defensive fallback if chat row was deleted mid-request.
            session = await get_or_create_chat_session_for_user(
                db_session,
                chat_id=chat_id,
                user_id=auth.user_id,
                search_space_id=int(search_space_id),
                meta={},
            )
        elif int(session.search_space_id) != int(search_space_id):
            raise HTTPException(status_code=400, detail="search_space_id mismatch for chat_id")

        await append_turn(
            db_session,
            chat_session_id=int(session.id),
            role="assistant",
            content=assistant_content,
            model=os.getenv("SOLVER_MODEL", "gpt-4o"),
        )
        await refresh_memory_summary(db_session, chat_session=session)
        session.updated_at = datetime.now(UTC)
        await db_session.commit()


def _append_assistant_turn_and_refresh(
    *,
    auth: AuthContext,
    chat_id: str,
    search_space_id: int,
    assistant_content: str,
) -> None:
    run_async(
        _append_assistant_turn_and_refresh_async(
            auth=auth,
            chat_id=chat_id,
            search_space_id=search_space_id,
            assistant_content=assistant_content,
        )
    )


def _save_attachments(attachments: Sequence[AttachmentIn]) -> List[str]:
    """Decode data URLs and write to runtime/uploads. Returns list of file paths."""
    paths: List[str] = []
    if not attachments:
        return paths

    outdir = get_runtime_dir() / "uploads"
    outdir.mkdir(parents=True, exist_ok=True)

    for att in attachments:
        m = DATA_URL_RE.match(att.data_url)
        if not m:
            log.warning("Skipping attachment with non data-url: %s", att.name)
            continue
        try:
            b = base64.b64decode(m.group("data").encode("utf-8"), validate=True)
        except Exception:
            log.debug("Strict base64 decode failed, using lenient fallback")
            # lenient decode fallback
            b = base64.b64decode(m.group("data").encode("utf-8"))
        # derive extension from mime
        ext = ""
        if "/" in att.mime:
            ext = "." + att.mime.split("/")[-1].lower().split(";")[0]
        fname = f"{uuid.uuid4().hex}_{att.name}".replace(" ", "_")
        if not os.path.splitext(fname)[1] and ext:
            fname += ext
        path = outdir / fname
        with open(path, "wb") as f:
            f.write(b)
        paths.append(str(path))
    return paths


def _iter_text(obj: Union[str, bytes, Iterable, Iterator]) -> Iterator[str]:
    """Normalize various return types to an iterator of text chunks."""
    if obj is None:
        yield ""
        return
    if isinstance(obj, bytes):
        yield obj.decode("utf-8", errors="ignore")
        return
    if isinstance(obj, str):
        yield obj
        return
    # Iterable / Iterator of unknown elements
    for x in obj:  # type: ignore
        if isinstance(x, bytes):
            yield x.decode("utf-8", errors="ignore")
        else:
            yield str(x)


def _structured_citations_from_bundle(
    bundle: Any, used_markers: Optional[List[str]]
) -> List[Dict[str, Any]]:
    if not used_markers:
        return []
    used_set = {m.strip() for m in used_markers if isinstance(m, str) and m.strip()}
    if not used_set:
        return []

    entries: List[Dict[str, Any]] = []
    id_to_row: Dict[str, Dict[str, Any]] = {}
    store_meta: Dict[str, Dict[str, Any]] = {}

    for sn in getattr(bundle, "snippets", []) or []:
        marker = getattr(sn, "citation_marker", "") or ""
        if marker not in used_set:
            continue
        sn_id = getattr(sn, "id", None)
        sn_type = getattr(sn, "type", "other") or "other"
        entries.append({"id": sn_id, "snippet": sn})
        if sn_id is not None:
            id_to_row[sn_id] = {
                "store_key": sn_type,
                "store_kind": sn_type,
                "source_path": getattr(sn, "source_path", ""),
            }
        if sn_type not in store_meta:
            store_meta[sn_type] = {"kind": sn_type}

    _, structured = format_citations(entries, id_to_row, store_meta)
    return structured


# -------- App --------
app = FastAPI(title="AI-TA HTTP Server", version="0.1.0")


@app.on_event("startup")
def _validate_startup_env() -> None:
    validate_required_env()

# CORS
cors_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
allow_origins = [o.strip() for o in cors_origins.split(",")] if cors_origins else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount AI-use reports router
try:
    from backend.reports.ai_use.routes import router as reports_router
except Exception:
    try:
        from reports.ai_use.routes import router as reports_router  # type: ignore
    except Exception:
        reports_router = None  # type: ignore

if reports_router is not None:
    app.include_router(reports_router)

# Mount chats router (simple transcript storage)
try:
    from backend.chats.routes import router as chats_router
except Exception:
    try:
        from chats.routes import router as chats_router  # type: ignore
    except Exception:
        chats_router = None  # type: ignore

if chats_router is not None:
    app.include_router(chats_router)


# -------- Knowledge endpoints --------
@app.get("/knowledge/subjects", response_model=List[KnowledgeSubjectOut])
def list_knowledge_subjects() -> List[KnowledgeSubjectOut]:
    manager = KnowledgeManager()
    try:
        subjects = manager.list_subjects()
    except Exception as exc:
        log.exception("Failed to list knowledge subjects")
        raise HTTPException(status_code=500, detail=str(exc))
    return subjects


class KnowledgeStoreIn(BaseModel):
    subject: str
    kind: str = Field(..., description="textbook|slides|homework|exams|other")
    title: str
    index_path: str = Field(..., description="Filesystem path to existing index directory")
    priority: Optional[int] = None


class KnowledgeStoreOut(BaseModel):
    id: str
    subject: str
    kind: str
    title: str
    index_path: str
    priority: int
    created_at: str


@app.get("/knowledge/stores", response_model=List[KnowledgeStoreOut])
def list_knowledge_stores(subject: str = "") -> List[KnowledgeStoreOut]:
    subject_clean = (subject or "").strip()
    if not subject_clean:
        raise HTTPException(status_code=400, detail="subject is required")
    manager = KnowledgeManager()
    try:
        stores = manager.list_stores(subject_clean)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # attach subject to each entry for response consistency
    out: List[KnowledgeStoreOut] = []
    for s in stores:
        out.append(
            KnowledgeStoreOut(
                id=str(s.get("id", "")),
                subject=subject_clean,
                kind=str(s.get("kind", "")),
                title=str(s.get("title", "")),
                index_path=str(s.get("index_path", "")),
                priority=int(s.get("priority", 0) or 0),
                created_at=str(s.get("created_at", "")),
            )
        )
    return out


@app.post("/knowledge/stores", response_model=KnowledgeStoreOut)
def register_knowledge_store(payload: KnowledgeStoreIn) -> KnowledgeStoreOut:
    manager = KnowledgeManager()
    try:
        entry = manager.register_store(
            payload.subject,
            kind=payload.kind,
            title=payload.title,
            index_path=Path(payload.index_path),
            priority=payload.priority,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return KnowledgeStoreOut(
        id=str(entry.get("id", "")),
        subject=str(entry.get("subject", payload.subject)),
        kind=str(entry.get("kind", payload.kind)),
        title=str(entry.get("title", payload.title)),
        index_path=str(entry.get("index_path", payload.index_path)),
        priority=int(entry.get("priority", payload.priority or 0) or 0),
        created_at=str(entry.get("created_at", "")),
    )


@app.get("/teacher/weeks", response_model=TeacherCourseOut)
def get_teacher_weeks(request: Request, search_space_id: int = Query(..., gt=0)) -> TeacherCourseOut:
    _require_course_membership(request, search_space_id=search_space_id, role="teacher")
    manager = _get_teacher_storage()
    try:
        payload = manager.list_course_by_search_space(search_space_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize_course_payload(payload)


@app.post("/teacher/weeks/current", response_model=TeacherCourseOut)
def set_teacher_current_week(payload: TeacherCurrentWeekIn, request: Request) -> TeacherCourseOut:
    _require_course_membership(
        request,
        search_space_id=int(payload.search_space_id),
        role="teacher",
    )
    manager = _get_teacher_storage()
    try:
        manager.set_current_week_by_search_space(payload.search_space_id, payload.current_week)
        data = manager.list_course_by_search_space(payload.search_space_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _serialize_course_payload(data)


@app.get("/teacher/retrieval-weights", response_model=TeacherRetrievalWeightsOut)
def get_teacher_retrieval_weights(
    request: Request,
    search_space_id: int = Query(..., gt=0),
) -> TeacherRetrievalWeightsOut:
    _require_course_membership(request, search_space_id=search_space_id, role="teacher")
    manager = _get_teacher_storage()
    try:
        weights = manager.get_retrieval_weights_by_search_space(search_space_id)
        course_payload = manager.list_course_by_search_space(search_space_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    defaults = get_env_weights()
    return TeacherRetrievalWeightsOut(
        search_space_id=search_space_id,
        course=str(course_payload.get("course", "")),
        weights=RetrievalWeightValues(**weights),
        defaults=RetrievalWeightValues(**defaults),
        bounds=WeightBoundsOut(min=WEIGHT_MIN, max=WEIGHT_MAX),
    )


@app.post("/teacher/retrieval-weights", response_model=TeacherRetrievalWeightsOut)
def update_teacher_retrieval_weights(
    payload: TeacherRetrievalWeightsUpdateIn,
    request: Request,
) -> TeacherRetrievalWeightsOut:
    _require_course_membership(
        request,
        search_space_id=int(payload.search_space_id),
        role="teacher",
    )
    manager = _get_teacher_storage()
    try:
        updated = manager.update_retrieval_weights_by_search_space(
            payload.search_space_id,
            payload.weights.to_dict(),
        )
        course_payload = manager.list_course_by_search_space(payload.search_space_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    defaults = get_env_weights()
    return TeacherRetrievalWeightsOut(
        search_space_id=int(payload.search_space_id),
        course=str(course_payload.get("course", "")),
        weights=RetrievalWeightValues(**updated),
        defaults=RetrievalWeightValues(**defaults),
        bounds=WeightBoundsOut(min=WEIGHT_MIN, max=WEIGHT_MAX),
    )


if _HAS_MULTIPART:

    @app.post("/knowledge/materials", response_model=KnowledgeMaterialOut)
    def upload_knowledge_material(
        subject: str = Form(...),
        title: str = Form(""),
        file: UploadFile = File(...),
    ) -> KnowledgeMaterialOut:
        subject_clean = (subject or "").strip()
        if not subject_clean:
            raise HTTPException(status_code=400, detail="subject is required")

        filename = file.filename or "knowledge-material.pdf"
        name_path = Path(filename)
        if name_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail="Only PDF knowledge materials are supported")

        resolved_title = (title or name_path.stem).strip() or name_path.stem
        tmp_dir = Path(tempfile.mkdtemp(prefix="knowledge_upload_"))
        tmp_path = tmp_dir / name_path.name

        try:
            with tmp_path.open("wb") as dest:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    dest.write(chunk)
            if tmp_path.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")

            manager = KnowledgeManager()
            material = manager.add_pdf_material(
                subject=subject_clean,
                pdf_path=tmp_path,
                title=resolved_title,
            )
        except HTTPException:
            raise
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            log.warning("Embedding knowledge material failed: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Failed to process knowledge material upload")
            raise HTTPException(status_code=500, detail="Failed to embed knowledge material") from exc
        finally:
            try:
                file.file.close()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return material


    @app.post("/teacher/upload", response_model=TeacherUploadOut)
    def upload_teacher_material(
        request: Request,
        search_space_id: int = Form(...),
        week: int = Form(...),
        kind: str = Form(...),
        title: str = Form(""),
        file: UploadFile = File(...),
    ) -> TeacherUploadOut:
        auth = _require_course_membership(
            request,
            search_space_id=int(search_space_id),
            role="teacher",
        )
        filename = file.filename or "teacher-upload.pdf"
        name_path = Path(filename)
        if name_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

        tmp_dir = Path(tempfile.mkdtemp(prefix="teacher_upload_"))
        tmp_path = tmp_dir / name_path.name

        try:
            with tmp_path.open("wb") as dest:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    dest.write(chunk)
            if tmp_path.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")

            manager = _get_teacher_storage()
            record = manager.record_upload_by_search_space(
                int(search_space_id),
                week=int(week),
                kind=kind,
                pdf_path=tmp_path,
                title=title or name_path.stem,
                uploaded_by=auth.user_id,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            log.exception("Teacher ingestion failed")
            raise HTTPException(status_code=500, detail=str(exc) or "Failed to process upload")
        except Exception as exc:
            log.exception("Failed to process teacher upload")
            raise HTTPException(status_code=500, detail=f"Failed to process upload: {exc}") from exc
        finally:
            try:
                file.file.close()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return TeacherUploadOut(**record.to_dict())

else:  # pragma: no cover - fallback when python-multipart missing

    @app.post("/knowledge/materials")
    async def upload_knowledge_material_unavailable() -> None:
        raise HTTPException(
            status_code=503,
            detail="File upload support requires the 'python-multipart' package.",
        )

    @app.post("/teacher/upload")
    async def upload_teacher_material_unavailable() -> None:
        raise HTTPException(
            status_code=503,
            detail="File upload support requires the 'python-multipart' package.",
        )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/classes")
def list_classes():
    """Return available search spaces for the class dropdown."""
    from .database.models import SearchSpace
    from .database.session import get_async_session, run_async
    from sqlalchemy import select as sa_select

    async def _fetch():
        async with get_async_session() as session:
            result = await session.execute(
                sa_select(SearchSpace).order_by(SearchSpace.name)
            )
            return result.scalars().all()

    try:
        spaces = run_async(_fetch())
    except Exception as exc:
        log.exception("Failed to load classes from aita_search_spaces")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load classes. Check SUPABASE_DB_URL, DB connectivity, and migrations. Error: {exc}",
        )
    return [
        {
            "id": s.id,
            "slug": s.slug,
            "name": s.name,
            "subject_name": s.subject_name,
        }
        for s in spaces
    ]


# ---------------------------------------------------------------------------
# pgvector retrieval helper
# ---------------------------------------------------------------------------

def _ask_pgvector(q_effective: str, workspace, weight_overrides: dict, cfg) -> "ResearchBundle":
    """Run the pgvector retrieval pipeline and return a ResearchBundle.

    Uses the shared background event loop so asyncpg connections stay alive.
    """
    from .config.contracts import ResearchBundle, ResearchMetadata
    from .retrieval.pipeline import retrieve_for_question
    from .retrieval.context_packer import _summarize_snippets
    from .database.session import get_async_session, run_async
    from .ai.main_ai import extract_and_filter_keywords

    search_space_id = int(workspace.metadata.get("search_space_id", 0))
    if not search_space_id:
        raise RuntimeError(
            "Workspace missing search_space_id — run migrations and "
            "set USE_PGVECTOR_RETRIEVAL=true only after seeding the DB."
        )

    token_budget = int(os.getenv("TOKEN_BUDGET", "6000"))

    # Extract keywords (role: hints appended to original question, not standalone targets)
    # extract_and_filter_keywords returns (context_summary, term_list)
    try:
        _ctx_summary, raw_keywords = extract_and_filter_keywords(q_effective)
        keywords: List[str] = []
        for entry in (raw_keywords or []):
            if isinstance(entry, dict):
                term = entry.get("term") or entry.get("keyword") or entry.get("name")
                if isinstance(term, str) and term.strip():
                    keywords.append(term.strip())
            elif isinstance(entry, str) and entry.strip():
                keywords.append(entry.strip())
    except Exception:
        keywords = []

    async def _run():
        async with get_async_session() as db_session:
            return await retrieve_for_question(
                query=q_effective,
                keywords=keywords,
                search_space_id=search_space_id,
                db_session=db_session,
                weight_overrides=weight_overrides or {},
                top_k=int(os.getenv("K_SEM", "20")),
                token_budget=token_budget,
            )

    snippets, diagnostics = run_async(_run())

    # Extract structured knowledge from snippet text (equations, glossary, etc.)
    equations, glossary, assumptions, _ = _summarize_snippets(snippets)

    allowed_markers = [sn.citation_marker for sn in snippets if sn.citation_marker]

    metadata = ResearchMetadata(
        keyword_iterations=[{"round": 1, "combined_query": diagnostics.get("combined_query", q_effective)}],
        found_terms=[],
        not_found_terms=[],
        attempted_terms=[q_effective],
        missing_terms=[],
        final_query=diagnostics.get("combined_query", q_effective),
        original_query=q_effective,
        concept_matches={},
        concept_match_details={},
        coverage_gaps=[],
        refinement_queries=[],
        subject=cfg.subject_name if cfg else "",
        allowed_markers=allowed_markers,
        hit_count_sem=diagnostics.get("hit_count_sem", 0),
    )

    return ResearchBundle(
        metadata=metadata,
        snippets=snippets,
        equations=equations,
        assumptions=assumptions,
        glossary=glossary,
        coverage_gaps=[],
        refinement_queries=[],
        used_ids=[sn.id for sn in snippets],
        stats=diagnostics,
        provenance={"source": "pgvector", **{k: str(v) for k, v in diagnostics.items()}},
        allowed_markers=allowed_markers,
        found_terms=[],
        not_found_terms=[],
        attempted_terms=[q_effective],
        subject=cfg.subject_name if cfg else "",
    )


# Intentionally sync `def` — FastAPI auto-threads sync endpoints.
# Do NOT change to `async def` unless the entire pipeline is made async.
@app.post("/ask")
def post_ask(payload: AskRequest, request: Request):
    """Accept a question and/or image attachments, stream back plain-text answer.

    Now supports image-only queries. Either a non-empty `question` OR at least
    one attachment must be provided. Image attachments are decoded and saved to
    `runtime/uploads/` and their file paths are passed along to the core.
    """
    # Validate input: allow (question) OR (attachments)
    q = (payload.question or "").strip()
    atts = payload.attachments or []
    if not q and not atts:
        raise HTTPException(status_code=400, detail="Provide a question or image attachments")

    chat_id = (payload.chat_id or "").strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")

    search_space_id = int(payload.search_space_id)
    auth = _require_course_membership(
        request,
        search_space_id=search_space_id,
    )

    if payload.doc_sets:
        raise HTTPException(
            status_code=400,
            detail="doc_sets overrides are disabled; configure materials within the class workspace.",
        )

    try:
        workspace = _get_workspace_manager().get(str(search_space_id))
    except WorkspaceNotFound:
        raise HTTPException(status_code=404, detail="Unknown search_space_id")
    except WorkspaceConfigError as exc:
        log.error("Workspace misconfiguration for search_space_id=%s: %s", search_space_id, exc)
        raise HTTPException(status_code=500, detail="Class workspace is misconfigured")
    except WorkspaceError as exc:  # pragma: no cover - defensive
        log.exception("Failed to load workspace for search_space_id=%s", search_space_id)
        raise HTTPException(status_code=500, detail="Failed to load class workspace") from exc

    class_name = (payload.class_name or "").strip() or workspace.class_name
    subject_name = workspace.subject_name or class_name

    teacher_storage: Optional[TeacherWeeklyStorage] = None
    try:
        teacher_storage = _get_teacher_storage()
    except Exception:
        log.warning("Failed to load teacher weekly storage")
        teacher_storage = None

    try:
        image_paths = _save_attachments(atts)
    except Exception as e:
        log.exception("Attachment decode failed")
        raise HTTPException(status_code=400, detail=f"Invalid attachments: {e}")

    # Augment question with image-derived keywords (mirrors backend.qa ask behavior)
    q_effective = q
    image_text = ""
    if image_paths:
        try:
            image_text = vision_transcribe(image_paths) or ""
        except Exception:
            log.warning("Vision transcription failed for uploaded images")
            image_text = ""
    if image_text:
        try:
            image_context = extract_keywords(image_text) or ""
        except Exception:
            log.warning("Keyword extraction from image text failed")
            image_context = ""
        fallback_image_query = " ".join(image_text.split())[:500]
        image_query = image_context.strip() if image_context.strip() else fallback_image_query
        if q_effective and image_query:
            q_effective = q_effective.rstrip() + " \n" + image_query
        elif image_query:
            q_effective = image_query

    chat_meta = {
        "search_space_id": search_space_id,
        "class_name": workspace.class_name,
        "subject_name": subject_name,
    }
    user_content = _user_turn_content(q_effective or q, atts)
    memory_context = ""
    try:
        memory_context = _load_memory_and_append_user_turn(
            auth=auth,
            chat_id=chat_id,
            search_space_id=search_space_id,
            user_content=user_content,
            attachments=atts,
            meta=chat_meta,
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to load chat memory or append user turn: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=_debug_error_detail("Failed to persist chat turn", exc),
        )

    if memory_context:
        current_prompt = q_effective.strip() or user_content
        q_effective = f"{memory_context}\n\nCurrent question:\n{current_prompt}".strip()

    weight_overrides = get_env_weights()
    weight_overrides.update(workspace.weight_overrides or {})
    for material in workspace.materials:
        if material.weight_override is not None:
            weight_overrides[material.kind] = material.weight_override
    if teacher_storage is not None:
        try:
            teacher_weights = teacher_storage.get_retrieval_weights_by_search_space(search_space_id)
        except Exception:
            log.warning("Failed to get teacher retrieval weights")
            teacher_weights = {}
        else:
            weight_overrides.update(teacher_weights)

    stdout_buffer = io.StringIO()
    answer_chunks: List[str] = []
    structured_citations: List[Dict[str, Any]] = []
    error_text: Optional[str] = None

    try:
        cfg = RequestConfig.from_env()
        cfg.set_subject(subject_name, "server")

        # Run retrieval and question parsing in parallel — parse_question
        # only needs q_effective, no dependency on retrieval results.
        # parse_question is submitted first (outside redirect_stdout) since
        # it only uses logging, not print(). _ask_pgvector is the function
        # that prints wire-log lines captured by redirect_stdout.
        with ThreadPoolExecutor(max_workers=2) as _pool:
            parse_future = (
                _pool.submit(parse_question, q_effective, subject=cfg.subject_name)
                if q_effective.strip()
                else None
            )

            with redirect_stdout(stdout_buffer):
                bundle = _ask_pgvector(
                    q_effective=q_effective,
                    workspace=workspace,
                    weight_overrides=weight_overrides,
                    cfg=cfg,
                )

            parsed_task: Optional[ParsedTask] = None
            if parse_future is not None:
                try:
                    parsed_task = parse_future.result()
                except Exception:
                    log.error("Question parsing failed", exc_info=True)
                    parsed_task = None

        if parsed_task is None:
            fallback_problem = q_effective.strip() or "Question"
            parsed_task = ParsedTask(
                problem_type=fallback_problem,
                asked_outputs=["answer"],
                asked_output_keys=["answer"],
            )

        solution = solve_with_bundle(
            parsed_task, bundle, subject=cfg.subject_name,
        )
        final = format_answer(
            solution, bundle, include_background=False,
            subject=cfg.subject_name,
        )
        answer_chunks.append(final.text)
        structured_citations = _structured_citations_from_bundle(
            bundle, getattr(final, "citations", [])
        )
    except Exception as e:
        log.exception("qa ask pipeline failed")
        error_text = f"[error] {e}"

    answer_text = "".join(answer_chunks).strip()
    if error_text and not answer_text:
        answer_text = error_text

    raw_logs = stdout_buffer.getvalue().splitlines()
    wire_logs = [line.strip() for line in raw_logs if line.strip().startswith("[Main AI") or line.strip().startswith("[Indexer AI")]

    payload_out = {
        "answer": answer_text,
        "logs": wire_logs,
        "citations": structured_citations,
    }

    assistant_turn = answer_text.strip() or error_text or "[error] Unknown error"
    try:
        _append_assistant_turn_and_refresh(
            auth=auth,
            chat_id=chat_id,
            search_space_id=search_space_id,
            assistant_content=assistant_turn,
        )
    except Exception:
        log.warning("Failed to persist assistant turn for chat_id=%s", chat_id, exc_info=True)

    return JSONResponse(payload_out)


# ---------------------------------------------------------------------------
# SSE streaming variant — same pipeline, but streams progress events so the
# frontend can show real-time status updates instead of a blank wait.
# ---------------------------------------------------------------------------

def _sse_event(event: str, data: dict) -> str:
    """Format a single Server-Sent Event."""
    return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/ask/stream")
async def post_ask_stream(payload: AskRequest, request: Request):
    """Streaming variant of /ask — returns SSE events with progress + final answer."""

    # --- Validation (identical to /ask) ------------------------------------
    q = (payload.question or "").strip()
    atts = payload.attachments or []
    if not q and not atts:
        raise HTTPException(status_code=400, detail="Provide a question or image attachments")

    chat_id = (payload.chat_id or "").strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")

    search_space_id = int(payload.search_space_id)
    auth = _require_course_membership(
        request,
        search_space_id=search_space_id,
    )

    if payload.doc_sets:
        raise HTTPException(
            status_code=400,
            detail="doc_sets overrides are disabled; configure materials within the class workspace.",
        )

    try:
        workspace = _get_workspace_manager().get(str(search_space_id))
    except WorkspaceNotFound:
        raise HTTPException(status_code=404, detail="Unknown search_space_id")
    except WorkspaceConfigError as exc:
        log.error("Workspace misconfiguration for search_space_id=%s: %s", search_space_id, exc)
        raise HTTPException(status_code=500, detail="Class workspace is misconfigured")
    except WorkspaceError as exc:
        log.exception("Failed to load workspace for search_space_id=%s", search_space_id)
        raise HTTPException(status_code=500, detail="Failed to load class workspace") from exc

    async def _generate():
        stream_loop = asyncio.get_running_loop()
        class_name = (payload.class_name or "").strip() or workspace.class_name
        subject_name = workspace.subject_name or class_name

        teacher_storage: Optional[TeacherWeeklyStorage] = None
        try:
            teacher_storage = _get_teacher_storage()
        except Exception:
            log.warning("Failed to load teacher weekly storage")

        try:
            image_paths = _save_attachments(atts)
        except Exception as e:
            log.exception("Attachment decode failed")
            yield _sse_event("error", {"message": f"Invalid attachments: {e}"})
            return

        q_effective = q
        image_text = ""
        if image_paths:
            yield _sse_event("status", {"stage": "vision", "message": "Reading image attachments..."})
            try:
                image_text = vision_transcribe(image_paths) or ""
            except Exception:
                log.warning("Vision transcription failed for uploaded images")
                image_text = ""
        if image_text:
            try:
                image_context = extract_keywords(image_text) or ""
            except Exception:
                log.warning("Keyword extraction from image text failed")
                image_context = ""
            fallback_image_query = " ".join(image_text.split())[:500]
            image_query = image_context.strip() if image_context.strip() else fallback_image_query
            if q_effective and image_query:
                q_effective = q_effective.rstrip() + " \n" + image_query
            elif image_query:
                q_effective = image_query

        chat_meta = {
            "search_space_id": search_space_id,
            "class_name": workspace.class_name,
            "subject_name": subject_name,
        }
        user_content = _user_turn_content(q_effective or q, atts)
        try:
            memory_context = await stream_loop.run_in_executor(
                None,
                lambda: _load_memory_and_append_user_turn(
                    auth=auth,
                    chat_id=chat_id,
                    search_space_id=search_space_id,
                    user_content=user_content,
                    attachments=atts,
                    meta=chat_meta,
                ),
            )
        except HTTPException as exc:
            yield _sse_event("error", {"message": exc.detail})
            return
        except Exception as exc:
            log.error("Failed to load chat memory or append user turn", exc_info=True)
            yield _sse_event(
                "error",
                {"message": _debug_error_detail("Failed to persist chat turn", exc)},
            )
            return

        if memory_context:
            current_prompt = q_effective.strip() or user_content
            q_effective = f"{memory_context}\n\nCurrent question:\n{current_prompt}".strip()

        weight_overrides = get_env_weights()
        weight_overrides.update(workspace.weight_overrides or {})
        for material in workspace.materials:
            if material.weight_override is not None:
                weight_overrides[material.kind] = material.weight_override
        if teacher_storage is not None:
            try:
                teacher_weights = teacher_storage.get_retrieval_weights_by_search_space(search_space_id)
            except Exception:
                log.warning("Failed to get teacher retrieval weights")
                teacher_weights = {}
            else:
                weight_overrides.update(teacher_weights)

        try:
            cfg = RequestConfig.from_env()
            cfg.set_subject(subject_name, "server")

            # --- Stage 1: Retrieval + parse (parallel) ---------------------
            yield _sse_event("status", {"stage": "retrieving", "message": "Searching course materials..."})

            bundle_future = stream_loop.run_in_executor(
                None, lambda: _ask_pgvector(
                    q_effective=q_effective,
                    workspace=workspace,
                    weight_overrides=weight_overrides,
                    cfg=cfg,
                )
            )
            parse_future = (
                stream_loop.run_in_executor(
                    None, lambda: parse_question(q_effective, subject=cfg.subject_name)
                )
                if q_effective.strip()
                else None
            )

            bundle = await bundle_future

            parsed_task: Optional[ParsedTask] = None
            if parse_future is not None:
                try:
                    parsed_task = await parse_future
                except Exception:
                    log.error("Question parsing failed", exc_info=True)
            if parsed_task is None:
                fallback_problem = q_effective.strip() or "Question"
                parsed_task = ParsedTask(
                    problem_type=fallback_problem,
                    asked_outputs=["answer"],
                    asked_output_keys=["answer"],
                )

            # --- Stage 2: Scoring + solution ------------------------------
            n_snippets = len(bundle.snippets) if bundle.snippets else 0
            yield _sse_event("status", {
                "stage": "analyzing",
                "message": f"Analyzing {n_snippets} relevant excerpt{'s' if n_snippets != 1 else ''}...",
            })

            solution = await stream_loop.run_in_executor(
                None, lambda: solve_with_bundle(parsed_task, bundle, subject=cfg.subject_name)
            )

            # --- Stage 3: Format ------------------------------------------
            yield _sse_event("status", {"stage": "formatting", "message": "Preparing answer..."})

            final = format_answer(
                solution, bundle, include_background=False,
                subject=cfg.subject_name,
            )
            answer_text = final.text or ""
            structured_citations = _structured_citations_from_bundle(
                bundle, getattr(final, "citations", [])
            )

            yield _sse_event("answer", {
                "answer": answer_text,
                "citations": structured_citations,
                "logs": [],
            })

            try:
                await stream_loop.run_in_executor(
                    None,
                    lambda: _append_assistant_turn_and_refresh(
                        auth=auth,
                        chat_id=chat_id,
                        search_space_id=search_space_id,
                        assistant_content=answer_text.strip() or "[empty answer]",
                    ),
                )
            except Exception:
                log.warning("Failed to persist assistant turn for chat_id=%s", chat_id, exc_info=True)

        except Exception as e:
            log.exception("SSE ask pipeline failed")
            error_message = f"[error] {e}"
            yield _sse_event("error", {"message": error_message})
            try:
                await stream_loop.run_in_executor(
                    None,
                    lambda: _append_assistant_turn_and_refresh(
                        auth=auth,
                        chat_id=chat_id,
                        search_space_id=search_space_id,
                        assistant_content=error_message,
                    ),
                )
            except Exception:
                log.warning("Failed to persist assistant error turn for chat_id=%s", chat_id, exc_info=True)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Optional: allow `python -m backend.server`
if __name__ == "__main__":
    import uvicorn  # type: ignore

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("backend.server:app", host="0.0.0.0", port=port, reload=True)
