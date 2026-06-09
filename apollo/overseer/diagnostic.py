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
bands, and the coverage map (which reference entries the student covered vs.
missed). Your job is to NARRATE the rubric's verdict — not to re-grade it.

HARD RULES — never violate:
- Do not contradict the rubric's per-axis scores. If an axis is A+, do not
  describe it as deficient; if an axis is F, do not describe it as strong.
- Frame every gap pedagogically: "you didn't explicitly walk Apollo through X" /
  "the teaching skipped X" / "Apollo had to infer X without being shown" — never
  as Apollo failing.
- Missing reference entries are FORMATIVE feedback (what to expand on next),
  not an accusation. Be encouraging.

Output format: a short, supportive report (6-12 sentences) for the student.
- Lead with the lowest-scoring PRESENT axis. Name it and what it means.
- Then call out specifically what they taught well (which covered entries).
- Explain what was missing and WHY it mattered — what Apollo's understanding
  lacks because that piece wasn't taught.
- End with a concrete next step tied to the weakest axis: re-teach that specific
  piece, or return to Hoot to study that concept.

Tone: diagnostic, supportive, not judgmental. Do not invent details. Do not add
physics beyond what the reference solution and coverage tell you."""


def generate_diagnostic(
    *,
    coverage: Dict[str, Any],
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
        narrative = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("diagnostic LLM soft-fail: %s", exc)
        narrative = (
            "[Diagnostic narrative unavailable — the grade above is still accurate.]"
        )

    narrative = _append_misconception_line(narrative, rubric)
    narrative = _append_negotiation_line(narrative, coverage)
    return narrative


def _append_negotiation_line(narrative: str, coverage: Dict[str, Any]) -> str:
    """Append a one-line summary of how many KG entries the student
    negotiated.

    Class 2 Phase 3 (P3.11). The pedagogical signal here is metacognitive:
    the student looked at Apollo's understanding and acted on it. We
    surface the count so the diagnostic can credit that behavior without
    re-litigating which entries were touched.

    Source: `coverage["negotiation_counts"]` (P3.4). Tolerates legacy
    coverage dicts (no key) by treating counts as zero.

    Output is empty when nothing was negotiated. Phrased generically —
    never names individual entries."""
    counts = coverage.get("negotiation_counts") or {}
    paraphrased = int(counts.get("paraphrased", 0) or 0)
    skipped = int(counts.get("skipped", 0) or 0)
    disputed = int(counts.get("disputed", 0) or 0)
    total = paraphrased + skipped + disputed
    if total == 0:
        return narrative

    parts: list[str] = []
    if paraphrased:
        parts.append(f"{paraphrased} paraphrased")
    if skipped:
        parts.append(f"{skipped} skipped")
    if disputed:
        parts.append(f"{disputed} disputed")
    breakdown = ", ".join(parts)

    line = (
        f"You negotiated {total} entr{'y' if total == 1 else 'ies'} with "
        f"Apollo: {breakdown}."
    )
    if narrative and not narrative.endswith("\n"):
        narrative += "\n\n"
    return narrative + line


def _append_misconception_line(narrative: str, rubric: Dict[str, Any]) -> str:
    """Append a deterministic one-liner about misconceptions navigated.

    Class 2 Phase 2 (P2.9). Visible to the student only when at least
    one misconception was detected during the attempt. Phrased to avoid
    naming the misconception itself — the student already saw the
    Socratic moves; this is a recap, not a re-revelation.

    No-op when no misconceptions fired; narrative returned unchanged.
    """
    block = rubric.get("misconception_corrected") or {}
    detected = int(block.get("detected", 0) or 0)
    if detected <= 0:
        return narrative
    resolved = int(block.get("resolved", 0) or 0)
    line = (
        f"During the conversation, you and Apollo navigated through "
        f"{detected} suspected misconception"
        f"{'s' if detected != 1 else ''}; you resolved {resolved} of them."
    )
    if narrative and not narrative.endswith("\n"):
        narrative += "\n\n"
    return narrative + line
