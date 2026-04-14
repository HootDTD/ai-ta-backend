"""Deterministic output filter — structural leakage barrier.

Algorithm: scan Apollo's draft for any word in the physics-stopword list
that does NOT appear in the student's message history or the current KG
(entry labels + content). First stopword found that isn't in the allowlist
triggers FilterRejectedError. NO FALLBACK — rejection is terminal; the
caller surfaces the error in the UI.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from apollo.errors import FilterRejectedError

_PHYSICS_STOPWORDS = frozenset({
    # Core concepts
    "bernoulli", "continuity", "viscosity", "viscous", "navier", "stokes",
    "compressible", "compressibility", "incompressible", "incompressibility",
    "turbulence", "turbulent", "laminar", "streamline", "streamlines",
    # Energy & dynamics adjacent
    "kinetic", "potential", "enthalpy", "entropy", "conservation",
    # Domain names
    "physics", "mechanics", "hydrodynamics", "aerodynamics", "thermodynamics",
    # Units
    "pascal", "pascals", "newton", "newtons", "joule", "joules",
})

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _tokenize(text: str) -> List[str]:
    return [m.group(0).lower().strip("'") for m in _WORD_RE.finditer(text)]


def _allowed_vocabulary(kg: Dict[str, List[Dict[str, Any]]], history: List[Dict[str, str]]) -> set[str]:
    allowed: set[str] = set()

    for msg in history:
        if msg.get("role") == "user":
            allowed.update(_tokenize(msg.get("content", "")))

    def _absorb(value: Any) -> None:
        if isinstance(value, str):
            allowed.update(_tokenize(value))
        elif isinstance(value, dict):
            for v in value.values():
                _absorb(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _absorb(v)

    for _type, entries in kg.items():
        for entry in entries:
            _absorb(entry)

    return allowed


def validate_or_raise(
    draft: str,
    kg: Dict[str, List[Dict[str, Any]]],
    history: List[Dict[str, str]],
) -> str:
    """Return the draft unchanged if clean. Raise FilterRejectedError on the
    first physics-stopword in the draft that isn't in the allowed vocabulary."""
    allowed = _allowed_vocabulary(kg, history)
    draft_tokens = _tokenize(draft)

    for token in draft_tokens:
        if token in _PHYSICS_STOPWORDS and token not in allowed:
            raise FilterRejectedError(rejected_term=token, draft=draft)

    return draft
