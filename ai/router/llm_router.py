from __future__ import annotations
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Stage2Decision:
    route: str
    retrieval_mode: str
    confidence: float
    reason: str


_HOOT_ROUTE_SCHEMA = {
    "name": "hoot_route",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["route", "retrieval_mode", "confidence", "reason"],
        "properties": {
            "route": {
                "type": "string",
                "enum": [
                    "conceptual_explainer", "stepwise_problem_solver",
                    "factual_lookup", "definition",
                    "study_guide_generator", "clarify",
                ],
            },
            "retrieval_mode": {
                "type": "string",
                "enum": ["NONE", "AUGMENT", "FRESH"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 200},
        },
    },
}


_SYSTEM_PROMPT = """\
You route a student's question for a course assistant.

Pick exactly one specialist:
- conceptual_explainer: explain how/why something works
- stepwise_problem_solver: multi-step worked solution
- factual_lookup: one short factual answer
- definition: define a term in 25-60 words
- study_guide_generator: bullet outlines, takeaways, practice questions
- clarify: only when the query is genuinely ambiguous

Then pick a retrieval_mode:
- NONE: the answer is fully covered by recent chat + cached snippets
- AUGMENT: partly covered; small fresh top-up retrieval would help
- FRESH: new topic or prior context insufficient

Use the recent turns and cached snippet titles below as evidence for retrieval_mode.
Return ONLY JSON matching the schema."""


class LLMRouter:
    def __init__(self, client, model: str | None = None) -> None:
        self._client = client
        self._model = model or os.environ.get("ROUTER_MODEL", "gpt-4o-mini")

    async def classify(
        self,
        *,
        query: str,
        recent_turns: list[dict],          # [{role, content}, ...] last 2-3 user turns
        cached_titles: list[str],
    ) -> Stage2Decision:
        user_block = (
            f"Query: {query}\n\n"
            f"Recent turns: {json.dumps(recent_turns)[:1500]}\n\n"
            f"Cached snippet titles: {json.dumps(cached_titles)[:1000]}"
        )
        resp = await self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_block},
            ],
            response_format={"type": "json_schema", "json_schema": _HOOT_ROUTE_SCHEMA},
        )
        payload = json.loads(resp.choices[0].message.content)
        return Stage2Decision(
            route=payload["route"],
            retrieval_mode=payload["retrieval_mode"],
            confidence=float(payload["confidence"]),
            reason=payload["reason"],
        )
