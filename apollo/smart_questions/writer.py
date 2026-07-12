"""Answer-blind wording for a planner-selected reference target."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from typing import Any, cast

from openai import OpenAI

from apollo.ontology import Node

_SAFE_FALLBACK = "I’m still missing one step—can you explain what I should do next?"


def _private_phrases(node: Node) -> list[str]:
    values = node.content.model_dump().values()
    return [
        str(value).strip() for value in values if isinstance(value, str) and len(value.strip()) >= 4
    ]


def _leaks_private_target(question: str, node: Node, student_words: Sequence[str]) -> bool:
    normalized_question = re.sub(r"\s+", " ", question).casefold()
    student_text = re.sub(r"\s+", " ", " ".join(student_words)).casefold()
    return any(
        phrase.casefold() in normalized_question and phrase.casefold() not in student_text
        for phrase in _private_phrases(node)
    )


def write_question(*, node: Node, transcript: Sequence[tuple[str, str]]) -> str:
    """Ask about the target dimension without supplying its answer."""
    student_words = [content for role, content in transcript if role == "student"]
    client = OpenAI()
    response = client.chat.completions.create(
        model=cast(Any, os.getenv("APOLLO_QUESTION_MODEL") or os.getenv("MAIN_MODEL") or "gpt-4o"),
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "Write exactly one short question in Apollo's confused-student voice. "
                    "The private target tells you what understanding is missing. Never state, "
                    "name, paraphrase, confirm, or hint at the target answer. Use only terms the "
                    "student already used. Ask for an explanation, relationship, reason, or next "
                    "step. Return the question only."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "private_target_type": node.node_type,
                        "private_target": node.content.model_dump(),
                        "student_words": student_words,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    question = (response.choices[0].message.content or "").strip()
    if not question or _leaks_private_target(question, node, student_words):
        return _SAFE_FALLBACK
    return question
