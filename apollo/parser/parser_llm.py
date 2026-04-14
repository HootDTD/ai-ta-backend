"""Parser: student utterance → structured KG entries via GPT-4o JSON mode.

Under no-fallback policy: if the utterance LOOKS like a teaching attempt
(contains equation-like syntax, or a term from the variable normalization
map, or is >=10 chars and non-conversational) and the LLM extracts zero
entries, we raise ParserCouldNotExtractError. Short acknowledgements
("ok", "yes") legitimately produce empty extractions and do NOT raise.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from openai import OpenAI

from apollo.errors import ParserCouldNotExtractError

_SYSTEM_PROMPT = """You extract structured knowledge-graph entries from a student's
explanation of a fluid-mechanics concept. Return ONLY a JSON object of the form:

{"entries": [ { "type": "equation"|"definition"|"condition"|"simplification"|"variable_mapping",
                "content": { ... type-specific fields ... } } ]}

For type=equation: content must have "symbolic" (a SymPy-parseable string using the
canonical symbols P, rho, v, A, h, g, Q, and subscripts like P1, v2 as underscore-free
identifiers; use Rational(1,2) for halves, ** for exponents, avoid unicode) and "label"
(short human name from what the student called it). Prefer zero-form: LHS - (RHS).

For type=condition: content must have "applies_when" (natural language) and "label".
For type=simplification: content must have "applies_when" and "transformation".
For type=definition: content must have "concept" and "meaning".
For type=variable_mapping: content must have "term" and "symbol".

Rules:
- Return ONLY what the student explicitly said. Do NOT add physics the student did not mention.
- If the student said nothing extractable, return {"entries": []}.
- Do not correct the student. If they said an equation wrong, extract it as stated.
- If the student is stating Bernoulli-style equality comparing two points/states, introduce
  subscripts (P1/v1/A1/h1 vs P2/v2/A2/h2) so the solver can relate the two states.
"""

_EQUATION_LIKE = re.compile(r"[=*/^+\-]|\d+\.?\d*|\^|\*\*")
_TRIVIAL_ACKS = {"ok", "okay", "yes", "no", "hmm", "hi", "hey", "thanks", "thx", "ty"}


def _is_non_trivial(utterance: str) -> bool:
    s = utterance.strip().lower()
    if len(s) < 10:
        return False
    if s in _TRIVIAL_ACKS:
        return False
    if _EQUATION_LIKE.search(utterance):
        return True
    keywords = ("pressure", "velocity", "density", "area", "height", "flow",
                "fluid", "equation", "bernoulli", "continuity", "energy",
                "incompressible", "horizontal", "pipe")
    return any(k in s for k in keywords)


def parse_utterance(utterance: str, model: str | None = None) -> List[Dict[str, Any]]:
    """Return list of KG entry dicts. Raises ParserCouldNotExtractError when
    a non-trivial utterance yields zero extractions."""
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
    except json.JSONDecodeError:
        if _is_non_trivial(utterance):
            raise ParserCouldNotExtractError(utterance=utterance)
        return []

    raw_entries = payload.get("entries", [])
    if not isinstance(raw_entries, list):
        raw_entries = []

    entries = [
        e for e in raw_entries
        if isinstance(e, dict) and "type" in e and "content" in e
    ]

    if not entries and _is_non_trivial(utterance):
        raise ParserCouldNotExtractError(utterance=utterance)

    return entries
