"""Coverage: compare a frozen KG against a problem's reference solution.

For string-matchable entry types (equation, condition, simplification,
definition, variable_mapping), coverage is binary: 'covered' or 'missing'.

For procedure_step entries, coverage is a 0-1 partial-credit score per
reference step, produced by a small LLM call asking whether any student
procedure_step describes the same action and approximate ordering.
Soft-fails to 0.0 on LLM exceptions so the report is never blocked.

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


def _equation_matches(kg_entry: Dict[str, Any], ref_content: Dict[str, Any]) -> bool:
    ref_label = (ref_content.get("label") or "").strip().lower()
    ref_sym = (ref_content.get("symbolic") or "").replace(" ", "")
    kg_label = (kg_entry.get("label") or "").strip().lower()
    kg_sym = (kg_entry.get("symbolic") or "").replace(" ", "")
    if ref_label and kg_label and ref_label == kg_label:
        return True
    if ref_sym and kg_sym and ref_sym == kg_sym:
        return True
    return False


def _condition_matches(kg_entry: Dict[str, Any], ref_content: Dict[str, Any]) -> bool:
    ref_label = (ref_content.get("label") or "").strip().lower()
    kg_label = (kg_entry.get("label") or "").strip().lower()
    ref_aw = (ref_content.get("applies_when") or "").strip().lower()
    kg_aw = (kg_entry.get("applies_when") or "").strip().lower()
    if ref_label and kg_label and ref_label == kg_label:
        return True
    if ref_aw and kg_aw and ref_aw == kg_aw:
        return True
    return False


def _simplification_matches(kg_entry: Dict[str, Any], ref_content: Dict[str, Any]) -> bool:
    ref_aw = (ref_content.get("applies_when") or "").strip().lower()
    kg_aw = (kg_entry.get("applies_when") or "").strip().lower()
    return bool(ref_aw and kg_aw and ref_aw == kg_aw)


_BINARY_MATCHERS = {
    "equation": _equation_matches,
    "condition": _condition_matches,
    "simplification": _simplification_matches,
}


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


def compute_coverage(
    kg: Dict[str, List[Dict[str, Any]]],
    reference_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return coverage result with per_step status and procedure_scores.

    per_step:          {ref_step.id: "covered" | "missing"}
    procedure_scores:  {ref_step.id: float}  (only for procedure_step refs)
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

        matcher = _BINARY_MATCHERS.get(ref_type)
        kg_entries = kg.get(ref_type, [])
        if matcher and any(matcher(e, ref_content) for e in kg_entries):
            per_step[ref_id] = "covered"
        elif ref_type in ("definition", "variable_mapping") and kg_entries:
            key = "concept" if ref_type == "definition" else "term"
            ref_key = (ref_content.get(key) or "").strip().lower()
            if ref_key and any((e.get(key) or "").strip().lower() == ref_key for e in kg_entries):
                per_step[ref_id] = "covered"
            else:
                per_step[ref_id] = "missing"
        else:
            per_step[ref_id] = "missing"

    return {"per_step": per_step, "procedure_scores": procedure_scores}
