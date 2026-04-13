"""Throwaway LLM-only parser for spike.

Takes a student utterance, asks GPT-4o to emit zero or more KG entries
in strict JSON, validates the JSON shape, returns a list of entries.
No regex layer, no confidence gating, no rejection logging. Week 3
replaces this with the hybrid parser.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from openai import OpenAI


_SYSTEM_PROMPT = """You extract structured knowledge-graph entries from a student's
explanation of a fluid-mechanics concept. Return ONLY a JSON object of the form:

{"entries": [ { "type": "equation"|"definition"|"condition"|"simplification"|"variable_mapping",
                "content": { ... type-specific fields ... } } ]}

For type=equation: content must have "symbolic" (a SymPy-parseable string using the
canonical symbols P, rho, v, A, h, g, Q, and subscripts like P1, v2 as underscore-free
identifiers) and "label" (short human name).

For type=condition: content must have "applies_when" (natural language) and "label".

For type=simplification: content must have "applies_when" and "transformation".

For type=definition: content must have "concept" and "meaning".

For type=variable_mapping: content must have "term" and "symbol".

Rules:
- Return ONLY what the student explicitly said. Do NOT add physics the student did not mention.
- If the student said nothing extractable, return {"entries": []}.
- Do not correct the student. If they said an equation wrong, extract it as stated.
"""


def parse_utterance(utterance: str, model: str | None = None) -> List[Dict[str, Any]]:
    """Return a list of KG entry dicts extracted from the student utterance."""
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": utterance},
        ],
        temperature=0.0,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and "type" in e and "content" in e]
    except json.JSONDecodeError:
        return []
