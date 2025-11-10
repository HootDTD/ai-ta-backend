import importlib
import io
import os
import re
import uuid
import base64
import logging
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Iterator, Iterable, Union, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from contextlib import contextmanager, redirect_stdout

# Import the callable core entrypoint using package‑relative import to avoid
# path issues when running under different working directories.
from .core import answer_question, _vision_transcribe
from .config import set_subject_name
from .orchestrator import Orchestrator
from .retriever import (
    load_assets,
    load_assets_all,
    ContextPack,
    ContextSnippet,
    answer as retriever_answer,
)
from .knowledge import KnowledgeManager
from .store_weights import WEIGHT_MIN, WEIGHT_MAX, get_env_weights
from .main_ai import extract_keywords
from .teacher_weekly import TeacherWeeklyStorage
from .workspaces import (
    WorkspaceConfigError,
    WorkspaceNotFound,
    WorkspaceError,
    build_workspace_manager,
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


def _resolve_index_path() -> str:
    configured = os.getenv("INDEX_DIR")
    if configured:
        return str(Path(configured).expanduser().resolve())
    default = Path(__file__).resolve().parent / "text-embeder/my_book_index_aero"
    return str(default)


def _legacy_class_config() -> Dict[str, Dict[str, Any]]:
    default_path = _resolve_index_path()
    return {
        "AAE 33300: Introduction to Fluid Mechanics": {
            "subject": "Fundamentals of Aerodynamics",
            "title": "Fundamentals of Aerodynamics",
            "kind": "textbook",
            "doc_sets": [default_path],
        }
    }

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


try:
    _workspace_manager = build_workspace_manager(_legacy_class_config())
except WorkspaceConfigError as exc:
    log.error("Failed to initialize workspace manager: %s", exc)
    raise


class AskRequest(BaseModel):
    question: str
    class_name: str = Field(..., alias="class", min_length=1, description="Class selection for knowledge routing")
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
    max_iters: Optional[int] = None
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
    course: str
    weights: RetrievalWeightValues
    defaults: RetrievalWeightValues
    bounds: WeightBoundsOut


class TeacherRetrievalWeightsUpdateIn(BaseModel):
    course: str = Field(..., alias="class")
    weights: RetrievalWeightValues


class TeacherCourseOut(BaseModel):
    course: str
    slug: str
    current_week: int
    weeks: List[TeacherWeekOut]


class TeacherCurrentWeekIn(BaseModel):
    course: str = Field(..., alias="class")
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
        course=str(payload.get("course", "")),
        slug=str(payload.get("slug", "")),
        current_week=int(payload.get("current_week", 1) or 1),
        weeks=weeks_out,
    )


def _save_attachments(attachments: Sequence[AttachmentIn]) -> List[str]:
    """Decode data URLs and write to ./tmp_uploads. Returns list of file paths."""
    paths: List[str] = []
    if not attachments:
        return paths

    outdir = os.path.abspath(os.path.join(os.getcwd(), "tmp_uploads"))
    os.makedirs(outdir, exist_ok=True)

    for att in attachments:
        m = DATA_URL_RE.match(att.data_url)
        if not m:
            log.warning("Skipping attachment with non data-url: %s", att.name)
            continue
        try:
            b = base64.b64decode(m.group("data").encode("utf-8"), validate=True)
        except Exception:
            # lenient decode fallback
            b = base64.b64decode(m.group("data").encode("utf-8"))
        # derive extension from mime
        ext = ""
        if "/" in att.mime:
            ext = "." + att.mime.split("/")[-1].lower().split(";")[0]
        fname = f"{uuid.uuid4().hex}_{att.name}".replace(" ", "_")
        if not os.path.splitext(fname)[1] and ext:
            fname += ext
        path = os.path.join(outdir, fname)
        with open(path, "wb") as f:
            f.write(b)
        paths.append(path)
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


@contextmanager
def _temp_env(vars: Dict[str, str]):
    old: Dict[str, Optional[str]] = {}
    try:
        for k, v in vars.items():
            old[k] = os.getenv(k)
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -------- App --------
app = FastAPI(title="AI-TA HTTP Server", version="0.1.0")

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

# Ensure DB tables exist (dev convenience)
try:
    from backend.reports.ai_use.models import init_db as _init_db  # type: ignore

    _init_db()
except Exception:
    pass


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
def get_teacher_weeks(class_name: str = Query(..., alias="class")) -> TeacherCourseOut:
    manager = _get_teacher_storage()
    try:
        payload = manager.list_course(class_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize_course_payload(payload)


@app.post("/teacher/weeks/current", response_model=TeacherCourseOut)
def set_teacher_current_week(payload: TeacherCurrentWeekIn) -> TeacherCourseOut:
    manager = _get_teacher_storage()
    try:
        manager.set_current_week(payload.course, payload.current_week)
        data = manager.list_course(payload.course)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _serialize_course_payload(data)


@app.get("/teacher/retrieval-weights", response_model=TeacherRetrievalWeightsOut)
def get_teacher_retrieval_weights(class_name: str = Query(..., alias="class")) -> TeacherRetrievalWeightsOut:
    manager = _get_teacher_storage()
    try:
        weights = manager.get_retrieval_weights(class_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    defaults = get_env_weights()
    return TeacherRetrievalWeightsOut(
        course=class_name,
        weights=RetrievalWeightValues(**weights),
        defaults=RetrievalWeightValues(**defaults),
        bounds=WeightBoundsOut(min=WEIGHT_MIN, max=WEIGHT_MAX),
    )


@app.post("/teacher/retrieval-weights", response_model=TeacherRetrievalWeightsOut)
def update_teacher_retrieval_weights(payload: TeacherRetrievalWeightsUpdateIn) -> TeacherRetrievalWeightsOut:
    manager = _get_teacher_storage()
    course = (payload.course or "").strip()
    if not course:
        raise HTTPException(status_code=400, detail="class is required")

    try:
        updated = manager.update_retrieval_weights(course, payload.weights.to_dict())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    defaults = get_env_weights()
    return TeacherRetrievalWeightsOut(
        course=course,
        weights=RetrievalWeightValues(**updated),
        defaults=RetrievalWeightValues(**defaults),
        bounds=WeightBoundsOut(min=WEIGHT_MIN, max=WEIGHT_MAX),
    )


if _HAS_MULTIPART:

    @app.post("/knowledge/materials", response_model=KnowledgeMaterialOut)
    async def upload_knowledge_material(
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
                    chunk = await file.read(1024 * 1024)
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
                await file.close()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return material


    @app.post("/teacher/upload", response_model=TeacherUploadOut)
    async def upload_teacher_material(
        course: str = Form(..., alias="class"),
        week: int = Form(...),
        kind: str = Form(...),
        title: str = Form(""),
        file: UploadFile = File(...),
    ) -> TeacherUploadOut:
        course_clean = (course or "").strip()
        if not course_clean:
            raise HTTPException(status_code=400, detail="class is required")
        filename = file.filename or "teacher-upload.pdf"
        name_path = Path(filename)
        if name_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

        tmp_dir = Path(tempfile.mkdtemp(prefix="teacher_upload_"))
        tmp_path = tmp_dir / name_path.name

        try:
            with tmp_path.open("wb") as dest:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    dest.write(chunk)
            if tmp_path.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")

            manager = _get_teacher_storage()
            record = manager.record_upload(
                course_clean,
                week=int(week),
                kind=kind,
                pdf_path=tmp_path,
                title=title or name_path.stem,
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
                await file.close()
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


@app.post("/ask")
def post_ask(payload: AskRequest):
    """Accept a question and/or image attachments, stream back plain-text answer.

    Now supports image-only queries. Either a non-empty `question` OR at least
    one attachment must be provided. Image attachments are decoded and saved to
    `tmp_uploads/` and their file paths are passed along to the core.
    """
    # Validate input: allow (question) OR (attachments)
    q = (payload.question or "").strip()
    atts = payload.attachments or []
    if not q and not atts:
        raise HTTPException(status_code=400, detail="Provide a question or image attachments")

    class_name = payload.class_name.strip()
    if not class_name:
        raise HTTPException(status_code=400, detail="Class is required")

    if payload.doc_sets:
        raise HTTPException(
            status_code=400,
            detail="doc_sets overrides are disabled; configure materials within the class workspace.",
        )

    try:
        workspace = _workspace_manager.get(class_name)
    except WorkspaceNotFound:
        raise HTTPException(status_code=404, detail="Unknown class selection")
    except WorkspaceConfigError as exc:
        log.error("Workspace misconfiguration for %s: %s", class_name, exc)
        raise HTTPException(status_code=500, detail="Class workspace is misconfigured")
    except WorkspaceError as exc:  # pragma: no cover - defensive
        log.exception("Failed to load workspace for class %s", class_name)
        raise HTTPException(status_code=500, detail="Failed to load class workspace") from exc

    subject_name = workspace.subject_name or class_name
    doc_sets_ordered = workspace.doc_sets()
    if not doc_sets_ordered:
        raise HTTPException(status_code=500, detail="No knowledge materials registered for this class")

    doc_sets_override: List[str] = []
    seen_paths: set[str] = set()
    for raw in doc_sets_ordered:
        try:
            resolved = str(Path(raw).resolve())
        except Exception:
            resolved = str(raw)
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        doc_sets_override.append(resolved)

    teacher_storage: Optional[TeacherWeeklyStorage] = None
    try:
        teacher_storage = _get_teacher_storage()
    except Exception:
        teacher_storage = None

    if teacher_storage is not None:
        try:
            teacher_paths = teacher_storage.resolve_week_doc_sets(class_name, include_previous=True)
        except Exception:
            teacher_paths = []
    else:
        teacher_paths = []
    if teacher_paths:
        combined: List[str] = list(doc_sets_override)
        combined.extend(teacher_paths)
        deduped: List[str] = []
        seen = set()
        for raw in combined:
            try:
                resolved = str(Path(raw).resolve())
            except Exception:
                resolved = str(raw)
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append(resolved)
        doc_sets_override = deduped

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
            image_text = _vision_transcribe(image_paths) or ""
        except Exception:
            image_text = ""
    if image_text:
        try:
            terms = extract_keywords(image_text) or []
        except Exception:
            terms = []
        image_query = " ".join(terms[:8]) if terms else " ".join(image_text.split())[:500]
        if q_effective and image_query:
            q_effective = q_effective.rstrip() + " \n" + image_query
        elif image_query:
            q_effective = image_query

    opts: Dict[str, str] = {}
    weight_overrides = dict(workspace.weight_overrides)
    for material in workspace.materials:
        if material.weight_override is not None:
            weight_overrides[material.kind] = material.weight_override
    if teacher_storage is not None:
        try:
            teacher_weights = teacher_storage.get_retrieval_weights(class_name)
        except Exception:
            teacher_weights = {}
        else:
            weight_overrides.update(teacher_weights)
    for kind, value in weight_overrides.items():
        env_key = f"RETRIEVAL_STORE_WEIGHT_{kind.upper()}"
        opts[env_key] = f"{value:.4f}"
    if payload.alias_miner is not None:
        opts["RETRIEVAL_ALIAS_MINER"] = "on" if payload.alias_miner else "off"
    if payload.proximity is not None:
        opts["RETRIEVAL_PROXIMITY"] = "on" if payload.proximity else "off"
    if payload.prf is not None:
        opts["RETRIEVAL_PRF"] = "on" if payload.prf else "off"
    if payload.def_bias is not None:
        opts["PACK_DEF_BIAS"] = "on" if payload.def_bias else "off"
    if payload.max_iters is not None:
        opts["RETRIEVAL_MAX_ITERS"] = str(payload.max_iters)
    if payload.sanitize is not None:
        opts["RETRIEVAL_SANITIZE"] = "on" if payload.sanitize else "off"

    stdout_buffer = io.StringIO()
    answer_chunks: List[str] = []
    error_text: Optional[str] = None

    try:
        with _temp_env(opts):
            with redirect_stdout(stdout_buffer):
                # Respect workspace subject for keyword filtering
                try:
                    set_subject_name(subject_name, "server")
                except Exception:
                    pass

                # Load retrieval assets mirroring CLI behavior
                try:
                    paths = [Path(p).resolve() for p in (doc_sets_override or [])]
                    if len(paths) > 1:
                        load_assets_all(paths)
                    elif paths:
                        load_assets(paths[0])
                except Exception as e:
                    raise RuntimeError(f"failed to load indexes: {e}")

                # Use the same pipeline as `python -m backend.qa ask`
                orch = Orchestrator()
                retrieval_opts = {
                    "doc_sets": doc_sets_override,
                    "k_sem": int(os.getenv("K_SEM", "30")),
                    "k_lex": int(os.getenv("K_LEX", "30")),
                    "token_budget": int(os.getenv("TOKEN_BUDGET", "6000")),
                }
                max_iters = int(os.getenv("RETRIEVAL_MAX_ITERS", str(payload.max_iters or 5)))

                bundle = orch._iterative_research(q_effective, retrieval_opts, max_iters)

                ctx_snippets = [
                    ContextSnippet(
                        id=sn.id,
                        type=sn.type,
                        page=sn.page,
                        section_path=sn.section_path,
                        text=sn.text,
                        figure_id=sn.figure_id,
                        why=sn.why,
                        source_path=sn.source_path,
                        doc_title=sn.doc_title,
                        doc_short=sn.doc_short,
                    )
                    for sn in bundle.snippets
                ]
                ctx = ContextPack(snippets=ctx_snippets, used_ids=bundle.used_ids, stats=bundle.stats)

                ans = retriever_answer(q_effective, ctx)
                answer_chunks.append(ans.text)
                # attach structured citations to local variable for response below
                result = ans  # for compatibility with existing citation extraction
    except Exception as e:
        log.exception("qa ask pipeline failed")
        error_text = f"[error] {e}"

    answer_text = "".join(answer_chunks).strip()
    if error_text and not answer_text:
        answer_text = error_text

    structured_citations: List[Dict[str, Any]] = []
    if "result" in locals():
        # Prefer structured citations from retriever Answer if available
        structured_citations = list(getattr(result, "structured_citations", []) or getattr(result, "citations", []) or [])

    raw_logs = stdout_buffer.getvalue().splitlines()
    wire_logs = [line.strip() for line in raw_logs if line.strip().startswith("[Main AI") or line.strip().startswith("[Indexer AI")]

    payload_out = {
        "answer": answer_text,
        "logs": wire_logs,
        "citations": structured_citations,
    }

    return JSONResponse(payload_out)


# Optional: allow `python -m backend.server`
if __name__ == "__main__":
    import uvicorn  # type: ignore

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("backend.server:app", host="0.0.0.0", port=port, reload=True)
