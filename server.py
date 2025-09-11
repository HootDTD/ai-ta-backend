import os
import re
import uuid
import base64
import logging
from typing import List, Optional, Sequence, Iterator, Iterable, Union

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Import the callable core entrypoint (created in backend/core.py)
try:
    from backend.core import answer_question
except Exception:
    from core import answer_question  # type: ignore

log = logging.getLogger("ai_ta_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# -------- Models --------
class AttachmentIn(BaseModel):
    name: str
    mime: str = Field(..., description="e.g., image/png")
    data_url: str = Field(..., description="data:<mime>;base64,<...>")


class AskRequest(BaseModel):
    question: str
    course_id: Optional[str] = None
    doc_sets: Optional[List[str]] = None
    attachments: Optional[List[AttachmentIn]] = Field(default_factory=list)


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


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/ask")
def post_ask(payload: AskRequest):
    """Accept a question and optional attachments, stream back plain-text answer."""
    if not payload.question or not payload.question.strip():
        raise HTTPException(status_code=400, detail="Missing 'question'")

    try:
        image_paths = _save_attachments(payload.attachments or [])
    except Exception as e:
        log.exception("Attachment decode failed")
        raise HTTPException(status_code=400, detail=f"Invalid attachments: {e}")

    def generate():
        try:
            result = answer_question(
                question=payload.question.strip(),
                image_paths=image_paths,
                course_id=payload.course_id,
                doc_sets=payload.doc_sets,
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
