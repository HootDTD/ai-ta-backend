"""Tiny in-memory KG for spike. One dict per session, no freeze logic."""
from __future__ import annotations

from typing import Any, Dict, List


def new_kg() -> Dict[str, List[Dict[str, Any]]]:
    return {"equation": [], "definition": [], "condition": [], "simplification": [], "variable_mapping": []}


def write_entries(kg: Dict[str, List[Dict[str, Any]]], entries: List[Dict[str, Any]]) -> None:
    for e in entries:
        t = e.get("type")
        if t in kg:
            kg[t].append(e.get("content", {}))


def summarize_for_apollo(kg: Dict[str, List[Dict[str, Any]]]) -> str:
    """Produce a bullet summary of what the student has taught so far.

    Apollo sees this as part of its context. No concept names introduced beyond
    what the student themselves mentioned — the labels come from student-sourced
    `label` fields only.
    """
    lines: List[str] = []
    for eq in kg["equation"]:
        lines.append(f"- equation ({eq.get('label','(no label)')}): {eq.get('symbolic','')}")
    for d in kg["definition"]:
        lines.append(f"- definition: {d.get('concept','?')} = {d.get('meaning','?')}")
    for c in kg["condition"]:
        lines.append(f"- condition: {c.get('applies_when','?')}")
    for s in kg["simplification"]:
        lines.append(f"- simplification: when {s.get('applies_when','?')}, {s.get('transformation','?')}")
    for vm in kg["variable_mapping"]:
        lines.append(f"- variable: {vm.get('term','?')} → {vm.get('symbol','?')}")
    return "\n".join(lines) if lines else "(the student hasn't taught me anything yet)"
