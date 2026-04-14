"""Coverage: compare a frozen KG against a problem's reference solution.

For each reference step, return a status:
  - 'covered'   : a KG entry of the right type matches (label or symbolic match).
  - 'missing'   : no KG entry matches.
"""
from __future__ import annotations

from typing import Any, Dict, List


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


_MATCHERS = {
    "equation": _equation_matches,
    "condition": _condition_matches,
    "simplification": _simplification_matches,
}


def compute_coverage(
    kg: Dict[str, List[Dict[str, Any]]],
    reference_steps: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Return {ref_step.id: 'covered' | 'missing'}."""
    result: Dict[str, str] = {}
    for step in reference_steps:
        ref_id = step["id"]
        ref_type = step["entry_type"]
        ref_content = step.get("content", {})
        matcher = _MATCHERS.get(ref_type)
        kg_entries = kg.get(ref_type, [])
        if matcher and any(matcher(e, ref_content) for e in kg_entries):
            result[ref_id] = "covered"
        elif ref_type in ("definition", "variable_mapping") and kg_entries:
            key = "concept" if ref_type == "definition" else "term"
            ref_key = (ref_content.get(key) or "").strip().lower()
            if ref_key and any((e.get(key) or "").strip().lower() == ref_key for e in kg_entries):
                result[ref_id] = "covered"
            else:
                result[ref_id] = "missing"
        else:
            result[ref_id] = "missing"
    return result
