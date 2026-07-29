"""Overseer.concept_inference: Hoot transcript -> concept_id.

Isolated LLM call. The LLM is given the transcript and the course's candidate
concepts (``{concept_id, display_name}``, drawn from ``app.concepts`` rows
scoped to the course). It must return exactly one matching ``concept_id`` (int)
from the provided set, or null. Apollo NEVER sees this call's output directly —
only the Overseer uses it to select a problem.

WU-3D §8A cutover: the candidate list changed from a hard-coded constant to a
course-scoped query result threaded in by the handler. This stays a pure LLM
call.
"""

from __future__ import annotations

import json

from openai import OpenAI

from apollo.errors import NoMatchingConceptError
from apollo.subjects.curriculum_db import ConceptRow
from config.models import MAIN_MODEL

_SYSTEM_PROMPT = """You are identifying which concept a student was most recently
learning about in a conversation. You will be given:
- the conversation transcript
- the list of candidate concepts a downstream tool supports, each as
  {"concept_id": <int>, "display_name": "<name>"}

Return ONLY a JSON object of the form: {"concept_id": <one of the provided concept_ids, or null>}

Rules:
- Pick the concept whose topic was MOST RECENTLY the focus of the conversation.
- If none of the provided concepts matches, return {"concept_id": null}.
- Do NOT invent concept ids. Use exactly one of the provided ids, or null.
"""


def infer_concept_id(
    *, transcript: str, candidates: list[ConceptRow], model: str | None = None
) -> int:
    """Infer the single best-matching ``concept_id`` from ``candidates``.

    Raises ``NoMatchingConceptError`` on null / unknown / invalid JSON (including
    the empty-candidates "course has no curriculum" path, where the LLM can only
    return null).
    """
    model = model or MAIN_MODEL
    client = OpenAI()
    user_content = json.dumps(
        {
            "transcript": transcript,
            "candidate_concepts": [
                {"concept_id": c.concept_id, "display_name": c.display_name} for c in candidates
            ],
        }
    )
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
    )
    raw = resp.choices[0].message.content or "{}"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NoMatchingConceptError(transcript_summary=transcript[:200]) from exc

    returned = payload.get("concept_id")
    allowed = {c.concept_id for c in candidates}
    # Reject bool explicitly: in Python `True == 1`, so a JSON `true` would
    # otherwise pass an `in {1, ...}` membership check.
    if isinstance(returned, bool) or not isinstance(returned, int) or returned not in allowed:
        raise NoMatchingConceptError(transcript_summary=transcript[:200])

    return returned
