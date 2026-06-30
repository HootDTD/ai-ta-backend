"""Re-score a clarified idea (spec §7). Reads (original ambiguous statement +
the student's committed clarification + the one candidate idea) and rules
correct / wrong / vague. This is NOT the deleted silent guess (which guessed
from nothing): it judges a committed answer to a pointed question — far more
decidable. DI'd + stubbed in tests; no live model in CI."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from apollo.agent._llm import main_chat
from apollo.errors import ResolutionUnavailableError

RescoreOutcome = Literal["confirmed", "refuted", "vague"]
_RESPONSE_FORMAT = {"type": "json_object"}
_PURPOSE = "clarification_rescore"
_VALID = {"confirmed", "refuted", "vague"}


@dataclass(frozen=True)
class ClarificationRequest:
    original_statement: str
    clarification_text: str
    candidate_display: str


ClarificationJudge = Callable[[ClarificationRequest], RescoreOutcome]


def _build_messages(request: ClarificationRequest) -> list[dict[str, str]]:
    system = (
        "You judge whether a student's clarified explanation matches a target idea. "
        'Reply strict JSON {"verdict": "confirmed"|"refuted"|"vague"}. '
        "confirmed = the clarification correctly expresses the target idea; "
        "refuted = it states the opposite or a wrong claim; "
        "vague = noncommittal / unclear. Judge meaning, not wording."
    )
    user = json.dumps(
        {
            "original_statement": request.original_statement,
            "clarification": request.clarification_text,
            "target_idea": request.candidate_display,
        }
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def default_clarification_judge(request: ClarificationRequest) -> RescoreOutcome:
    try:
        raw = main_chat(
            purpose=_PURPOSE,
            messages=_build_messages(request),
            response_format=_RESPONSE_FORMAT,
            temperature=0.0,
        )
        verdict = str(json.loads(raw or "{}").get("verdict", "vague"))
        return verdict if verdict in _VALID else "vague"  # unknown -> no credit
    except ResolutionUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ResolutionUnavailableError(
            stage="clarification_rescore", last_error=str(exc)
        ) from exc


def rescore_clarification(
    *,
    original_statement: str,
    clarification_text: str,
    candidate_display: str,
    judge: ClarificationJudge,
) -> RescoreOutcome:
    return judge(
        ClarificationRequest(
            original_statement=original_statement,
            clarification_text=clarification_text,
            candidate_display=candidate_display,
        )
    )
