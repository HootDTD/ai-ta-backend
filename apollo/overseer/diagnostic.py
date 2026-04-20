"""Overseer.diagnostic — student-facing narrative aligned with the rubric verdict.

The diagnostic LLM does NOT decide the grade. The rubric (computed in
apollo/overseer/rubric.py) is the verdict. This module produces a short
natural-language report that explains the verdict — leading with the
lowest-scoring axis, calling out what broke, and ending with a concrete
next step."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from openai import OpenAI

_LOG = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are the Overseer's diagnostic narrator. The student just taught an
ignorant agent (Apollo) to solve a specific problem. A deterministic rubric has
already graded the student — you have the full rubric scores, per-axis letter
bands, and the coverage map. You also have the solver's result and the problem's
reference solution.

Your job is to NARRATE the rubric's verdict — not to re-grade it. Do not decide
the verdict; the rubric has already done that. Narrate it.

Output format: a short, supportive report (6-12 sentences) for the student.
- Lead with the lowest-scoring axis. Explicitly name the axis the student is
  weakest on and what that means for their teaching.
- Call out specifically what they taught well (which covered entries) ONLY
  AFTER acknowledging the weakest axis.
- Explain what was missing and, critically, WHY it mattered — what chain of
  reasoning broke because that piece wasn't taught.
- End with a concrete next step tied to the weakest axis: re-teach that
  specific piece, or return to Hoot to study that concept.
- DO NOT open with "Apollo solved it!" even if the solver succeeded. Solver
  success is a side indicator, not the verdict. If the solver reached the
  answer but the rubric is below B, acknowledge that the student got to the
  number but did not teach the process well.

Tone: diagnostic, not judgmental. Use "Apollo couldn't..." not "you failed...".
Do not invent details. Do not add physics beyond what the reference solution
and coverage tell you."""


def generate_diagnostic(
    *,
    coverage: Dict[str, Any],
    solver_result: Dict[str, Any],
    reference_steps: List[Dict[str, Any]],
    problem_text: str,
    rubric: Dict[str, Any],
    model: str | None = None,
) -> str:
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    client = OpenAI()

    user_payload = {
        "problem": problem_text,
        "rubric": rubric,
        "coverage": coverage,
        "solver_result": {
            "status": solver_result.get("status"),
            "missing_variables": solver_result.get("missing_variables", []),
            "value": str(solver_result.get("value")) if solver_result.get("value") is not None else None,
        },
        "reference_required_entries": [
            {
                "id": s["id"],
                "type": s["entry_type"],
                "label": s.get("content", {}).get("label"),
                "action": s.get("content", {}).get("action"),
            }
            for s in reference_steps
        ],
    }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
            temperature=0.4,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("diagnostic LLM soft-fail: %s", exc)
        return "[Diagnostic narrative unavailable — the grade above is still accurate.]"
