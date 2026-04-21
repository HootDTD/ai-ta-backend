"""Coverage: compare a frozen KG against a problem's reference solution.

All non-procedure entry types (equation, condition, simplification, definition,
variable_mapping) are matched via a single LLM semantic judgment per reference
entry against the KG entries of the same type. Students phrase things in their
own words, so string equality is not adequate — the matcher judges by meaning.
Soft-fails to 'missing' on LLM exceptions so the report is never blocked.

For procedure_step entries, coverage is a 0-1 partial-credit score per reference
step from its own LLM call. Soft-fails to 0.0.

Return shape:
  {
    "per_step":          {ref_step.id: "covered" | "missing"},
    "procedure_scores":  {ref_step.id: float in [0, 1]}  # only procedure_step ids
  }
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from openai import OpenAI

_LOG = logging.getLogger(__name__)


_BINARY_MATCHER_PROMPT = """You are grading whether any student-taught knowledge-graph
entry semantically covers a single reference entry. Students phrase things in their own
words, so judge by meaning, not wording.

Return ONLY a JSON object of the form: {"covered": true} or {"covered": false}.

Guidance by entry_type:
- equation: two equations are equivalent if they express the same physical relationship.
  A student equation that omits a common non-zero factor the reference keeps (e.g.
  "A1*v1 - A2*v2" vs reference "rho*A1*v1 - rho*A2*v2") still covers the reference,
  because rho is a non-zero constant and both express mass conservation. Sign flips
  and algebraic rearrangements of the same equation are equivalent.
- condition: any student condition asserting the same physical assumption covers the
  reference (e.g. reference "density is constant" is covered by student "incompressible
  flow" or "incompressibility").
- simplification: any student simplification performing the same geometric or physical
  reduction covers the reference (e.g. reference applies_when "h1 == h2" is covered by
  student "the pipe is horizontal, so h1 = h2").
- definition: any student definition of the same concept covers the reference.
- variable_mapping: any student mapping of the same term to the same symbol covers the
  reference.

If no student entry expresses the reference's meaning, return {"covered": false}.
"""


def _binary_match(
    ref_entry: Dict[str, Any],
    kg_entries: List[Dict[str, Any]],
    *,
    entry_type: str,
    model: str | None = None,
) -> bool:
    """LLM-based semantic coverage check. Soft-fails to False on exception."""
    if not kg_entries:
        return False
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    try:
        client = OpenAI()
        payload = {
            "entry_type": entry_type,
            "reference_entry": ref_entry,
            "student_entries": kg_entries,
        }
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _BINARY_MATCHER_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return bool(parsed.get("covered", False))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("binary matcher soft-fail for %s: %s", entry_type, exc)
        return False


_PROCEDURE_MATCHER_PROMPT = """You are grading whether a student's procedure step
covers a reference procedure step. Score how well the student's action matches
the reference's action, with partial credit.

Return ONLY a JSON object of the form: {"score": <float in [0.0, 1.0]>}

Scoring guide:
- 1.0: student describes the same action with the same ordering/intent.
- 0.7-0.9: same action but vague on ordering or missing minor detail.
- 0.4-0.6: partial overlap — student describes part of the action but misses key intent.
- 0.1-0.3: tangentially related.
- 0.0: no match — student did not describe this step.

Consider the student's action semantically, not word-for-word. Ordering matters if the reference step has an order; if so, the student's step at the same order is the best candidate but not required.

If multiple student steps are provided, base your score on the single student step that best matches the reference step. Do not average across student steps.
"""


def _procedure_match_score(
    ref_content: Dict[str, Any],
    kg_procedure_steps: List[Dict[str, Any]],
    *,
    model: str | None = None,
) -> float:
    """LLM-based semantic match score in [0, 1]. Soft-fails to 0.0 on exception."""
    if not kg_procedure_steps:
        return 0.0
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    try:
        client = OpenAI()
        payload = {
            "reference_step": {
                "order": ref_content.get("order"),
                "action": ref_content.get("action"),
                "uses_equations": ref_content.get("uses_equations", []),
                "purpose": ref_content.get("purpose"),
            },
            "student_steps": [
                {
                    "order": s.get("order"),
                    "action": s.get("action"),
                    "uses_equations": s.get("uses_equations", []),
                    "purpose": s.get("purpose"),
                }
                for s in kg_procedure_steps
            ],
        }
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _PROCEDURE_MATCHER_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        score = float(parsed.get("score", 0.0))
        return max(0.0, min(1.0, score))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("procedure matcher soft-fail: %s", exc)
        return 0.0


_BINARY_TYPES = ("equation", "condition", "simplification")


def compute_coverage(
    kg: Dict[str, List[Dict[str, Any]]],
    reference_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return coverage result with per_step status and procedure_scores.

    per_step:          {ref_step.id: "covered" | "missing"}
    procedure_scores:  {ref_step.id: float}  (only for procedure_step refs)

    Reference entries of types not graded by the rubric (definition,
    variable_mapping) are skipped — they don't appear in per_step.
    """
    per_step: Dict[str, str] = {}
    procedure_scores: Dict[str, float] = {}

    kg_procedure_steps = kg.get("procedure_step", [])

    for step in reference_steps:
        ref_id = step["id"]
        ref_type = step["entry_type"]
        ref_content = step.get("content", {})

        if ref_type == "procedure_step":
            score = _procedure_match_score(ref_content, kg_procedure_steps)
            procedure_scores[ref_id] = score
            per_step[ref_id] = "covered" if score >= 0.5 else "missing"
            continue

        if ref_type in _BINARY_TYPES:
            kg_entries = kg.get(ref_type, [])
            covered = _binary_match(ref_content, kg_entries, entry_type=ref_type)
            per_step[ref_id] = "covered" if covered else "missing"
        # Other ref types are ungraded — intentionally skipped.

    return {"per_step": per_step, "procedure_scores": procedure_scores}
