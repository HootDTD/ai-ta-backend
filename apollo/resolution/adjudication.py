"""WU-3C2 — the single LLM adjudication call for the post-tier remainder (§5 step 5).

ONE ``main_chat`` call per attempt MAX, for ALL remaining ambiguous nodes at
once: candidate list = the constrained closed set, "return empty when unsure".
A returned key that is NOT in the candidate set is a hallucination ->
``ResolutionInvalidOutputError`` (hard, §5). A transient/infra failure of the
call -> ``ResolutionUnavailableError(stage='llm_adjudication')`` (must NOT void
the grade). An empty remainder makes NO call.

``main_chat`` is REUSED from ``apollo.agent._llm`` (consume-only). Tests patch
``apollo.resolution.adjudication.main_chat`` so no live OpenAI call ever fires;
the live path is reachable only when the caller passes
:func:`main_chat_adjudicator` explicitly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from apollo.agent._llm import main_chat
from apollo.errors import ResolutionInvalidOutputError, ResolutionUnavailableError
from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)

_RESPONSE_FORMAT = {"type": "json_object"}
_PURPOSE = "resolution_adjudication"


@dataclass(frozen=True)
class ResolutionLLMRequest:
    """The single batched adjudication request: the remaining node texts +
    the closed candidate vocabulary."""

    nodes: tuple[tuple[str, str], ...]          # (node_id, surface_text)
    candidates: tuple[tuple[str, str], ...]     # (canonical_key, display_name)


# An adjudicator maps a request to {node_id: canonical_key} for the nodes it is
# confident about. Returning fewer keys than nodes is "return empty when unsure".
ResolutionLLMReply = dict[str, str]
Adjudicator = Callable[[ResolutionLLMRequest], ResolutionLLMReply]


def _build_messages(request: ResolutionLLMRequest) -> list[dict[str, str]]:
    candidate_lines = "\n".join(
        f"- {key}: {name}" for key, name in request.candidates
    )
    node_lines = "\n".join(
        f"- {node_id}: {text}" for node_id, text in request.nodes
    )
    system = (
        "You map each student statement to AT MOST ONE canonical key from the "
        "provided closed candidate list. Return STRICT JSON "
        '{"resolutions": {"<node_id>": "<canonical_key>"}}. Omit a node '
        "entirely when unsure — never guess, never invent a key."
    )
    user = (
        f"Candidate keys:\n{candidate_lines}\n\n"
        f"Student statements:\n{node_lines}\n\n"
        'Respond with {"resolutions": {...}} only.'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def main_chat_adjudicator(request: ResolutionLLMRequest) -> ResolutionLLMReply:
    """The real one-call adjudicator: a single ``main_chat`` (gpt-4o,
    temperature 0) returning strict JSON. Surfaces a transient failure as
    ``ResolutionUnavailableError(stage='llm_adjudication')`` (NO FALLBACK)."""
    try:
        raw = main_chat(
            purpose=_PURPOSE,
            messages=_build_messages(request),
            response_format=_RESPONSE_FORMAT,
            temperature=0.0,
        )
        parsed = json.loads(raw or "{}")
        resolutions = parsed.get("resolutions", {})
        return {str(k): str(v) for k, v in resolutions.items()}
    except ResolutionUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a named infra error
        raise ResolutionUnavailableError(
            stage="llm_adjudication", last_error=str(exc)
        ) from exc


def adjudicate(
    remaining: list[Node],
    candidates: tuple[Candidate, ...],
    *,
    adjudicator: Adjudicator,
) -> dict[str, Candidate]:
    """Resolve the post-tier remainder with AT MOST ONE adjudication call.

    Returns ``{node_id: Candidate}`` for the nodes the adjudicator confidently
    mapped. Nodes the adjudicator omits stay unresolved (not in the dict). A
    returned key absent from ``candidates`` raises
    ``ResolutionInvalidOutputError``. An empty remainder makes NO call."""
    if not remaining:
        return {}

    by_key = {c.canonical_key: c for c in candidates}
    allowed = tuple(by_key)
    request = ResolutionLLMRequest(
        nodes=tuple((n.node_id, student_surface_text(n)) for n in remaining),
        candidates=tuple((c.canonical_key, c.display_name) for c in candidates),
    )

    _LOG.info("resolution_adjudication nodes=%d", len(remaining))
    reply = adjudicator(request)

    resolved: dict[str, Candidate] = {}
    for node_id, returned_key in reply.items():
        if returned_key not in by_key:
            _LOG.info(
                "resolution_llm_invalid returned=%s node_id=%s", returned_key, node_id
            )
            raise ResolutionInvalidOutputError(
                returned_key=returned_key, allowed_keys=allowed
            )
        resolved[node_id] = by_key[returned_key]
    return resolved
