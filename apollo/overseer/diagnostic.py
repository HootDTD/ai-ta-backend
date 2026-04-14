"""Overseer.diagnostic — student-facing gap report via isolated LLM call.

Sees the Overseer's full context (coverage, solver trace, reference
solution, problem text). Produces a short natural-language report the
student reads directly. Does NOT write to Apollo's context."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from openai import OpenAI

_SYSTEM_PROMPT = """You are the Overseer's diagnostic module. The student just taught an
ignorant agent (Apollo) to solve a specific problem. You have the full truth:
the problem, the reference solution's required knowledge entries, which ones
the student covered vs. missed, and whether Apollo's solve attempt succeeded.

Produce a concise, supportive diagnostic report (6-12 sentences) for the student:
- Lead with the outcome (solved / stuck) in plain language.
- Call out specifically what they taught well (coverage = covered entries).
- Call out what was missing and, critically, WHY it mattered — what chain of
  reasoning broke because that piece wasn't taught.
- End with a concrete next step: re-teach the missing piece, or move on, or
  return to Hoot to study specifically that concept.

Tone: diagnostic, not judgmental. Use "Apollo couldn't..." not "you failed...".
Do not invent details. Do not add physics beyond what the reference solution
and coverage tell you."""


def generate_diagnostic(
    *,
    coverage: Dict[str, str],
    solver_result: Dict[str, Any],
    reference_steps: List[Dict[str, Any]],
    problem_text: str,
    model: str | None = None,
) -> str:
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    client = OpenAI()

    user_payload = {
        "problem": problem_text,
        "coverage": coverage,
        "solver_result": {
            "status": solver_result.get("status"),
            "missing_variables": solver_result.get("missing_variables", []),
            "value": str(solver_result.get("value")) if solver_result.get("value") is not None else None,
        },
        "reference_required_entries": [
            {"id": s["id"], "type": s["entry_type"], "label": s.get("content", {}).get("label")}
            for s in reference_steps
        ],
    }

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        temperature=0.4,
    )
    return resp.choices[0].message.content or ""
