import importlib
import os
import re
import uuid
import base64
import logging
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Iterator, Iterable, Union, Dict

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from contextlib import contextmanager

# Import the callable core entrypoint using package‑relative import to avoid
# path issues when running under different working directories.
from .core import answer_question
from .knowledge import KnowledgeManager

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


class AskRequest(BaseModel):
    question: str
    subject: str = Field(..., min_length=1, description="Subject name for knowledge routing")
    course_id: Optional[str] = None
    doc_sets: Optional[List[str]] = None
    attachments: Optional[List[AttachmentIn]] = Field(default_factory=list)
    alias_miner: Optional[bool] = None
    proximity: Optional[bool] = None
    prf: Optional[bool] = None
    def_bias: Optional[bool] = None
    max_iters: Optional[int] = None
    sanitize: Optional[bool] = None


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




# -------- Utils --------
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)


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

else:  # pragma: no cover - fallback when python-multipart missing

    @app.post("/knowledge/materials")
    async def upload_knowledge_material_unavailable() -> None:
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

    subject = (payload.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")

    try:
        image_paths = _save_attachments(atts)
    except Exception as e:
        log.exception("Attachment decode failed")
        raise HTTPException(status_code=400, detail=f"Invalid attachments: {e}")

    opts: Dict[str, str] = {}
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

    def generate():
        try:
            with _temp_env(opts):
                result = answer_question(
                    question=q,
                    image_paths=image_paths,
                    course_id=payload.course_id,
                    doc_sets=payload.doc_sets,
                    subject=subject,
                )
                for chunk in _iter_text(result):
                    yield chunk
        except Exception as e:
            log.exception("answer_question failed")
            yield f"\n[error] {e}"

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# Optional: allow `python -m backend.server`
if __name__ == "__main__":
    import uvicorn  # type: ignore

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("backend.server:app", host="0.0.0.0", port=port, reload=True)
