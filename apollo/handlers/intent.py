"""Chat intent classifier — AgentTutor-style state machine for /chat.

Item #5 of the V3 checklist. Replaces the button-only flow:
  - Conversational "I'm done teaching" now reaches `handle_done`.
  - Restart / next / return-to-Hoot / help intents are also classified
    so a future patch can wire them through the same confirmation gate.

Two-stage routing:
  1. If the session has a `pending_intent` (e.g. "done"), check whether
     the new utterance is a confirmation. Yes -> execute. No -> clear the
     pending state and treat as a new turn.
  2. Otherwise classify the new utterance. If `done` lands above the
     confidence threshold, set pending_intent and reply with a
     confirmation prompt. All other intents fall through to the normal
     teaching path (low risk; future patches can add explicit handlers).

Soft-fails CLOSED: classifier exception => intent="teaching" with low
confidence. Better to let Apollo respond than to hijack the turn.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Final, Literal

from apollo.agent._llm import cheap_chat
from apollo.subjects import ConceptDefinition

_LOG = logging.getLogger(__name__)

Intent = Literal[
    "teaching",
    "done",
    "restart",
    "next",
    "return_to_hoot",
    "help",
    "off_topic",
]

ALL_INTENTS: Final[tuple[Intent, ...]] = (
    "teaching", "done", "restart", "next", "return_to_hoot", "help", "off_topic",
)

# Threshold above which a non-teaching intent triggers the confirmation
# gate. Below threshold we fall through to the teaching path — silent
# misclassification is preferable to hijacking a teaching turn.
INTENT_CONFIDENCE_THRESHOLD: Final[float] = 0.7

# How many recent turns the classifier sees as context.
_HISTORY_TAIL_LEN: Final[int] = 4


_AFFIRMATION_PATTERNS = (
    re.compile(r"\b(yes|yep|yeah|sure|ok|okay|please|do it|go ahead|grade me)\b", re.I),
    re.compile(r"\b(let'?s do it|sounds good|that'?s right|correct|confirm)\b", re.I),
)
_NEGATION_PATTERNS = (
    re.compile(r"\b(no|nope|nah|not yet|wait|cancel|hold on|never mind|nevermind)\b", re.I),
)


@dataclass(frozen=True)
class IntentVerdict:
    intent: Intent
    confidence: float
    reason: str | None


@dataclass(frozen=True)
class ConfirmationVerdict:
    affirmed: bool
    rejected: bool

    @property
    def ambiguous(self) -> bool:
        return not (self.affirmed or self.rejected)


_CLASSIFIER_PROMPT = """You classify a student utterance during a tutoring
session where the student is supposed to be TEACHING the assistant about a
concept.

Possible intents:
- teaching: the student is explaining, defining, writing equations, or
  describing a procedure for the concept. Default when ambiguous.
- done: the student is signalling they are finished teaching and want to
  be graded. Phrases like "ok I'm done", "that's all I have", "your turn",
  "go ahead and grade me".
- restart: the student wants to start the current problem over.
- next: the student wants to move to a different problem.
- return_to_hoot: the student wants to leave Apollo and go back to the
  main Hoot tutor.
- help: the student is asking the system for help with the UI or process,
  not asking about the subject.
- off_topic: the student is talking about something unrelated.

Return ONLY a JSON object:
{"intent": "<one of the above>", "confidence": <float in [0, 1]>,
 "reason": "<one short sentence>"}

Conservative bias: when in doubt, return "teaching".
"""


def _safe_intent(value: object) -> Intent:
    if isinstance(value, str) and value in ALL_INTENTS:
        return value  # type: ignore[return-value]
    return "teaching"


def _safe_confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def classify_intent(
    *,
    utterance: str,
    history: list[dict[str, str]],
    concept: ConceptDefinition,
) -> IntentVerdict:
    """Single-utterance intent classification. Soft-fails to teaching."""
    payload = {
        "concept_id": concept.concept_id,
        "subject_id": concept.subject_id,
        "history_tail": [
            {"role": m.get("role"), "content": m.get("content")}
            for m in history[-_HISTORY_TAIL_LEN:]
        ],
        "utterance": utterance,
    }
    try:
        raw = cheap_chat(
            purpose="intent_classifier",
            messages=[
                {"role": "system", "content": _CLASSIFIER_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("intent classifier soft-fail: %s", exc)
        return IntentVerdict(intent="teaching", confidence=0.0, reason=None)

    return IntentVerdict(
        intent=_safe_intent(parsed.get("intent")),
        confidence=_safe_confidence(parsed.get("confidence", 0.0)),
        reason=str(parsed.get("reason")) if parsed.get("reason") else None,
    )


def detect_confirmation(utterance: str) -> ConfirmationVerdict:
    """Cheap deterministic check for a yes/no answer to a confirmation
    prompt. No LLM call — keeps the gate fast on the second turn.

    Returns affirmed=True for clear yeses, rejected=True for clear noes,
    both False (ambiguous) otherwise — caller falls through.
    """
    text = utterance.strip()
    if not text:
        return ConfirmationVerdict(affirmed=False, rejected=False)
    affirmed = any(p.search(text) for p in _AFFIRMATION_PATTERNS)
    rejected = any(p.search(text) for p in _NEGATION_PATTERNS)
    if affirmed and rejected:
        # Mixed signal — bail out, let teaching path handle.
        return ConfirmationVerdict(affirmed=False, rejected=False)
    return ConfirmationVerdict(affirmed=affirmed, rejected=rejected)


def confirmation_prompt_for(intent: Intent) -> str:
    """User-facing confirmation prompt that Apollo emits when an intent
    above threshold is detected. Stays in Apollo's confused-student
    voice — no concept-naming."""
    prompts = {
        "done": "It sounds like you're done teaching me — should I try to use what you've taught me to grade myself?",
        "restart": "It sounds like you want to start over — should I throw away what you've taught me so far?",
        "next": "It sounds like you'd like a different problem — should we move on?",
        "return_to_hoot": "It sounds like you want to head back to your main tutor — should I close this out?",
        "help": "It sounds like you're asking about how this works rather than teaching me — would a quick explanation help?",
    }
    return prompts.get(intent, "")


__all__ = [
    "ALL_INTENTS",
    "INTENT_CONFIDENCE_THRESHOLD",
    "ConfirmationVerdict",
    "Intent",
    "IntentVerdict",
    "classify_intent",
    "confirmation_prompt_for",
    "detect_confirmation",
]
