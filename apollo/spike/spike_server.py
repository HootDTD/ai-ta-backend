"""Throwaway FastAPI app for the Week 2 spike.

Run locally: uvicorn apollo.spike.spike_server:app --reload --port 8765
Open in browser: http://localhost:8765/
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from apollo.spike.spike_apollo import apollo_reply
from apollo.spike.spike_kg import new_kg, summarize_for_apollo, write_entries
from apollo.spike.spike_parser import parse_utterance
from apollo.spike.spike_solver import solve_problem_01


app = FastAPI(title="Apollo Spike")

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# In-memory session store. Sessions evaporate on server restart.
_SESSIONS: Dict[str, Dict] = {}


def _to_zero_form(expr: str) -> str:
    """Convert 'LHS = RHS' to '(LHS) - (RHS)'. Pass through strings without '='.

    Raises ValueError if the expression contains more than one '=' (e.g.,
    chained equalities like 'a = b = c') to avoid silently producing
    malformed output for the SymPy parser.
    """
    if "=" not in expr:
        return expr
    parts = expr.split("=")
    if len(parts) != 2:
        raise ValueError(f"_to_zero_form: expected exactly one '=' in {expr!r}")
    lhs, rhs = parts
    return f"({lhs.strip()}) - ({rhs.strip()})"


class StartResponse(BaseModel):
    session_id: str
    apollo_greeting: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    apollo_reply: str
    kg_entries_added: int


class DoneResponse(BaseModel):
    problem_text: str
    solver_result: Dict


@app.get("/")
def root() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/session", response_model=StartResponse)
def start_session() -> StartResponse:
    sid = str(uuid4())
    greeting = apollo_reply(
        history=[{"role": "user", "content": "Hi! I'm about to start teaching you."}],
        kg_summary="(the student hasn't taught me anything yet)",
    )
    _SESSIONS[sid] = {
        "kg": new_kg(),
        "history": [
            {"role": "user", "content": "Hi! I'm about to start teaching you."},
            {"role": "assistant", "content": greeting},
        ],
    }
    return StartResponse(session_id=sid, apollo_greeting=greeting)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    sess = _SESSIONS.get(req.session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    entries = parse_utterance(req.message)
    write_entries(sess["kg"], entries)
    sess["history"].append({"role": "user", "content": req.message})
    reply = apollo_reply(
        history=sess["history"],
        kg_summary=summarize_for_apollo(sess["kg"]),
    )
    sess["history"].append({"role": "assistant", "content": reply})
    return ChatResponse(apollo_reply=reply, kg_entries_added=len(entries))


@app.post("/session/{session_id}/done", response_model=DoneResponse)
def done(session_id: str) -> DoneResponse:
    sess = _SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Convert KG equations/conditions into the shape solver expects.
    kg_for_solver = {
        "equations": [_to_zero_form(e["symbolic"]) for e in sess["kg"]["equation"] if "symbolic" in e],
        "conditions": [c.get("applies_when", "") for c in sess["kg"]["condition"]],
    }
    result = solve_problem_01(kg_for_solver)
    # Stringify sympy values so JSON can serialize.
    if "value" in result:
        result["value"] = str(result["value"])
    problem_text = (
        "Water (ρ=1000 kg/m³) flows through a horizontal pipe. "
        "At section 1 the area is 0.01 m², pressure 200 000 Pa, velocity 2.0 m/s. "
        "At section 2 the area is 0.005 m². What is P₂?"
    )
    return DoneResponse(problem_text=problem_text, solver_result=result)


@app.get("/session/{session_id}/kg")
def inspect_kg(session_id: str):
    sess = _SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sess["kg"]
