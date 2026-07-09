"""LLM-as-judge leakage check — semantic stage of the output filter.

The deterministic pre-filter in `output_filter` catches verbatim mentions
of named laws and forbidden concept terms. This judge catches the
paraphrase class — e.g. "speed times area is constant" leaks the
continuity equation by description without naming it.

Pattern: stepwise verification (Macina et al., EMNLP 2024). Verify is a
separate call from generate; the judge sees the full draft, the leakage
policy, and the vocabulary the student has actually introduced.

Default: `gpt-4o-mini` via `apollo.agent._llm.cheap_chat`. Tests inject
a stub via the `judge` parameter on `validate_or_raise`.

Confidence gate: leaks=true is acted on only when confidence >= 0.6.
"When Verification Hurts" (arXiv 2603.27076) shows verifiers degrade
outcomes when upstream is already reliable; the gate avoids overfiring
on benign drafts.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from apollo.agent._llm import cheap_chat
from apollo.subjects import ConceptDefinition

_LOG = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD: float = 0.6


_JUDGE_SYSTEM_PROMPT = """You are auditing a tutoring assistant called Apollo.
Apollo is being TAUGHT by a student. Apollo plays a confused learner who
knows nothing about the subject and may only reference what the student has
introduced in this session.

You will receive:
- the concept Apollo is being taught about (concept_id),
- the conversation history (everything the student has typed),
- a structured summary of what the student has taught Apollo,
- Apollo's draft reply.

Apollo MAY:
1. paraphrase using words and symbols the student has typed,
2. use symbols that appear in the structured summary,
3. use terms the student introduced via variable mappings.

Apollo MUST NOT:
1. name a concept, equation, law, or principle the student has not named
   (e.g. "Bernoulli's principle", "the continuity equation",
   "Navier-Stokes", "conservation of energy") — including in another
   language,
2. mention the subject domain (e.g. "fluid mechanics", "physics") unless
   the student named it,
3. paraphrase a canonical concept by its textbook description if the
   student has not done so first (e.g. saying "speed times area is
   constant" without the student saying it).

Return ONLY a JSON object of the form:
{"leaks": <bool>, "offending_phrase": <str or null>,
 "reason": <str or null>, "confidence": <float in [0, 1]>}

`offending_phrase` should be the shortest substring of the draft that
demonstrates the leak. `reason` is a one-sentence rationale. If leaks is
false, both may be null and confidence should reflect how clean the draft
appears.
"""


@dataclass(frozen=True)
class JudgeVerdict:
    leaks: bool
    offending_phrase: str | None
    reason: str | None
    confidence: float


class LeakageJudge(Protocol):
    """Dependency-injectable judge — tests pass a stub here."""

    def __call__(
        self,
        *,
        draft: str,
        concept: ConceptDefinition,
        history: list[dict[str, str]],
        kg_summary: str,
    ) -> JudgeVerdict: ...


def llm_leakage_judge(
    *,
    draft: str,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
) -> JudgeVerdict:
    """Default judge — runs the cheap model. Soft-fails CLOSED on parse
    errors (i.e. returns leaks=false with low confidence) so a transient
    JSON glitch doesn't block legitimate Apollo replies. The deterministic
    pre-filter remains the safety net for unambiguous violations."""
    payload = {
        "concept_id": concept.concept_id,
        "subject_id": concept.subject_id,
        "history": [
            {"role": m.get("role"), "content": m.get("content")}
            for m in history
        ],
        "kg_summary": kg_summary,
        "draft": draft,
    }
    try:
        raw = cheap_chat(
            purpose="leakage_judge",
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("leakage_judge soft-fail (open) on parse error: %s", exc)
        return JudgeVerdict(leaks=False, offending_phrase=None,
                            reason=None, confidence=0.0)

    leaks = bool(parsed.get("leaks", False))
    offending = parsed.get("offending_phrase")
    reason = parsed.get("reason")
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return JudgeVerdict(
        leaks=leaks,
        offending_phrase=str(offending) if offending else None,
        reason=str(reason) if reason else None,
        confidence=max(0.0, min(1.0, confidence)),
    )


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "JudgeVerdict",
    "LeakageJudge",
    "llm_leakage_judge",
]
