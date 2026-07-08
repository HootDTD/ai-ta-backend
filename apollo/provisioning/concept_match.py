"""Closed-list concept matching (reversed provisioning).

The course carries a PREMADE concept list; each scraped problem is CLASSIFIED
against it instead of minting new concepts. Protocol (measured 95.0% low /
96.7% with medium retry on the calc-2 authored corpus —
apollo/provisioning/corpora/calc2/authored/results_authored.md): the model is
BLIND to provenance; sees problem_text + the full list (slug/name/desc); emits
primary + secondary + confidence + rationale; NO_MATCH is allowed and NEVER
force-matched (the orchestrator holds NO_MATCH problems for teacher review).

Retry ONCE at reasoning_effort='medium' when pass 1 (a) is unparseable, (b) is
the known self-contradiction slip (primary=NO_MATCH while the rationale names a
listed concept), or (c) names a slug outside the list. ``chat_fn`` is injected
(Tier-1 tests: NO network).

This module also owns ``norm_slug``, the hyphen/underscore/case-insensitive
slug key shared with ``scripts/seed_premade_concepts.py`` — a hyphenated list
slug matches a registry-seeded underscore row instead of duplicating it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from apollo.subjects.curriculum_db import RegisteredConcept

__all__ = ["ConceptMatch", "build_match_schema", "match_concept", "norm_slug"]

_LOG = logging.getLogger(__name__)

_NO_MATCH = "NO_MATCH"

_MATCH_SYSTEM_PROMPT = (
    "You classify ONE mathematics/science problem against a COURSE'S CLOSED "
    "LIST of concepts. Pick the single concept whose core technique the "
    'problem is designed to exercise as "primary" (its slug EXACTLY as '
    'listed). List up to 3 other plausible slugs in "secondary". If NO '
    'listed concept fits, output primary="NO_MATCH" — do NOT force a fit. '
    "IMPORTANT: if your rationale concludes a listed concept applies, that "
    "concept must BE your primary — never name a fitting concept and still "
    'answer NO_MATCH. Respond ONLY as JSON: {"primary": <slug or '
    'NO_MATCH>, "secondary": [<slug>, ...], "confidence": <0..1>, '
    '"rationale": <one or two sentences>}.'
)


def norm_slug(slug: str) -> str:
    """Hyphen/underscore/case-insensitive slug key.

    The premade-list seeder and the matcher share this so a hyphenated list
    slug (``integration-by-parts``) matches a registry-seeded underscore row
    (``integration_by_parts``) instead of duplicating it.
    """
    return slug.strip().lower().replace("-", "_")


class ConceptMatch(BaseModel):
    """One matching outcome. ``concept_id is None`` <=> ``no_match``.

    ``slug`` is the REGISTERED row's spelling (not the model's echo), so
    downstream persistence always uses the course's own vocabulary.
    """

    concept_id: int | None
    slug: str | None
    secondary: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    rationale: str = ""
    no_match: bool
    retried: bool = False


def build_match_schema() -> dict:
    """Strict json_schema envelope for the match response."""
    return {
        "name": "concept_match",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "primary": {"type": "string"},
                "secondary": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["primary", "secondary", "confidence", "rationale"],
            "additionalProperties": False,
        },
    }


def _concept_list_block(concepts: Sequence[RegisteredConcept]) -> str:
    lines = []
    for c in concepts:
        desc = f": {c.description}" if c.description else ""
        lines.append(f"- {c.slug} — {c.display_name}{desc}")
    return "\n".join(lines)


def _parse(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("primary"), str):
        return None
    return parsed


def _rationale_names_a_concept(rationale: str, concepts: Sequence[RegisteredConcept]) -> bool:
    """The known self-contradiction slip: rationale names a listed concept
    while primary is NO_MATCH (seen on ibp-03 in the measured protocol)."""
    text = rationale.lower()
    for c in concepts:
        name = c.display_name.lower()
        slug_words = norm_slug(c.slug).replace("_", " ")
        if (name and name in text) or (slug_words and slug_words in text):
            return True
    return False


async def match_concept(
    problem_text: str,
    concepts: Sequence[RegisteredConcept],
    *,
    chat_fn: Callable[..., str],
) -> ConceptMatch:
    """Classify one problem against the course's registered concepts.

    ``chat_fn`` is main-tier-shaped: ``chat_fn(purpose=, messages=,
    response_format=, reasoning_effort=) -> str`` (inject ``metered_chat.main``).
    """
    by_norm = {norm_slug(c.slug): c for c in concepts}
    user = json.dumps(
        {"problem_text": problem_text, "concepts": _concept_list_block(concepts)},
        sort_keys=True,
    )
    messages = [
        {"role": "system", "content": _MATCH_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

    def _ask(effort: str) -> dict | None:
        raw = chat_fn(
            purpose="concept_match",
            messages=messages,
            response_format={"type": "json_schema", "json_schema": build_match_schema()},
            reasoning_effort=effort,
        )
        return _parse(raw)

    retried = False
    parsed = _ask("low")
    needs_retry = (
        parsed is None
        or (
            str(parsed.get("primary")) == _NO_MATCH
            and _rationale_names_a_concept(str(parsed.get("rationale") or ""), concepts)
        )
        or (
            str(parsed.get("primary")) != _NO_MATCH
            and norm_slug(str(parsed.get("primary") or "")) not in by_norm
        )
    )
    if needs_retry:
        retried = True
        _LOG.info(
            "concept_match_retry",
            extra={"event": "concept_match_retry", "pass1_primary": (parsed or {}).get("primary")},
        )
        parsed = _ask("medium")

    if parsed is None:
        return ConceptMatch(concept_id=None, slug=None, no_match=True, retried=retried)

    primary = str(parsed.get("primary") or "").strip()
    concept = by_norm.get(norm_slug(primary)) if primary != _NO_MATCH else None
    secondary = [str(s) for s in (parsed.get("secondary") or []) if isinstance(s, str)]
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    rationale = str(parsed.get("rationale") or "")
    if concept is None:
        # NO_MATCH (clean, or a persistent hallucinated slug): NEVER force-matched.
        return ConceptMatch(
            concept_id=None,
            slug=None,
            secondary=secondary,
            confidence=confidence,
            rationale=rationale,
            no_match=True,
            retried=retried,
        )
    return ConceptMatch(
        concept_id=concept.concept_id,
        slug=concept.slug,
        secondary=secondary,
        confidence=confidence,
        rationale=rationale,
        no_match=False,
        retried=retried,
    )
