"""Overseer.concept_inference: Hoot transcript → concept_cluster_id.

Isolated LLM call. The LLM is given the transcript and the list of
concept clusters Apollo has problems for. It must return exactly one
matching cluster_id or null. Apollo NEVER sees this call's output
directly — only the Overseer uses it to select a problem.
"""
from __future__ import annotations

import json
import os
from typing import List

from openai import OpenAI

from apollo.errors import NoMatchingConceptError

_SYSTEM_PROMPT = """You are identifying which concept cluster a student was most
recently learning about in a conversation. You will be given:
- the conversation transcript
- the list of concept clusters that a downstream tool supports

Return ONLY a JSON object of the form: {"cluster_id": "<one of the provided cluster ids, or null>"}

Rules:
- Pick the cluster whose topic was MOST RECENTLY the focus of the conversation.
- If none of the provided clusters matches, return {"cluster_id": null}.
- Do NOT invent cluster ids. Use exactly one of the provided ids, or null.
"""


def infer_concept_cluster(*, transcript: str, available_clusters: List[str], model: str | None = None) -> str:
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    client = OpenAI()
    user_content = json.dumps({
        "transcript": transcript,
        "available_clusters": list(available_clusters),
    })
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

    cluster = payload.get("cluster_id")
    if cluster is None or cluster not in available_clusters:
        raise NoMatchingConceptError(transcript_summary=transcript[:200])

    return cluster
