"""Answer-blind wording for a planner-selected reference target.

The writer never sees the reference node — no content, no node id (ids like
``proc_char_information_overload`` carry the answer in the name), no node
type. Its only steering is the evaluator's guarded ``ask_hint`` nudge plus
the public surface: the problem statement and the student's own words. The
controller runs the deterministic leak guard on the returned question.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any, cast

from openai import OpenAI

SAFE_FALLBACK = "I’m still missing one step—can you explain what I should do next?"


def write_question(*, nudge: str, problem_text: str, transcript: Sequence[tuple[str, str]]) -> str:
    """Ask about the nudged part of the problem without knowing its answer."""
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
                    "The private nudge names which part of the problem to ask about next. "
                    "Ask for an explanation, reason, relationship, example, or next step "
                    "on that part, using only terms the student already used or wording "
                    "from the problem statement. Never answer the question yourself, and "
                    "never introduce concepts, facts, or terms that appear in neither. "
                    "Return the question only."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "nudge": nudge,
                        "problem": problem_text,
                        "student_words": student_words,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    question = (response.choices[0].message.content or "").strip()
    return question or SAFE_FALLBACK
