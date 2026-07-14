"""Overseer.diagnostic — student-facing narrative aligned with the rubric verdict.

The diagnostic LLM does NOT decide the grade. The rubric (computed in
apollo/overseer/rubric.py) is the verdict. This module produces a short
natural-language report that explains the verdict — leading with the
lowest-scoring axis, calling out what broke, and ending with a concrete
next step.

2026-07-10 (topic-score design spec section 4): when a ``TopicScoreResult``
is passed in AND ``APOLLO_TOPIC_SCORE_SERVED`` is on, ``generate_diagnostic``
replaces the axis-based prompt above with the ledger-grounded prompt built by
``apollo.overseer.topic_narrative.build_topic_narrative_prompt`` — every claim
in the narrative is traceable to a topic/misconception the deterministic
ledger actually holds (kills the axis path's hallucination class). Flag OFF
(or no ``topic_score`` argument) leaves this function's prompt/output unchanged
apart from the final internals sanitizer, which is a no-op on clean prose."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from typing import Any

from openai import OpenAI

from apollo.overseer.misconception_detector.config import topic_score_served_enabled
from apollo.overseer.topic_narrative import build_topic_narrative_prompt, sanitize_narrative
from apollo.overseer.topic_score import TopicScoreResult

_LOG = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Write feedback directly to a student who just taught Apollo how to solve a
specific problem. The deterministic assessment is already complete. Explain the result in a way
that helps the student improve; do not re-grade it.

HARD RULES — never violate:
- Address the student only as "you" and "your." Never say "the student" or refer to the student
  as "they/their." Sound like a coach who heard the explanation, not a report for a teacher.
- Do not contradict the rubric's per-axis scores. If an axis is A+, do not
  describe it as deficient; if an axis is F, do not describe it as strong.
- Frame every gap pedagogically: "you didn't explicitly walk Apollo through X" /
  "the teaching skipped X" / "Apollo had to infer X without being shown" — never
  as Apollo failing.
- Missing reference entries are formative feedback (what to expand on next), not an accusation.
- Reference entries describe the IDEAL solution, not what the student said. Never present a
  reference detail (a name, date, title, equation, or term) as something the student personally
  stated; credit covered entries in general terms only.
- Synthesize instead of listing every missing entry. Prioritize at most two related, high-value
  gaps and explain what adding them would do for Apollo's understanding.
- Never mention a misconception category when no misconception was detected. In particular,
  never say that no misconceptions or errors were recorded.
- Avoid audit language such as "partially covered," "entirely missing," "ledger," and "rubric."

Output format: two short paragraphs followed by exactly one final line beginning "Next step:".
- Lead with a specific strength from the covered entries and why it helped Apollo understand.
- Then explain the one or two most valuable improvements and why they matter.
- Make the next step a concrete revision the student can say, show, connect, compare, or
  illustrate. Never use the vague instruction "focus on understanding."
- Do not repeat the score or letter grade and do not add a generic conclusion.

Tone: diagnostic, supportive, not judgmental. Do not invent details. Do not add
physics beyond what the reference solution and coverage tell you."""


def generate_diagnostic(
    *,
    coverage: dict[str, Any],
    reference_steps: list[dict[str, Any]],
    problem_text: str,
    rubric: dict[str, Any],
    model: str | None = None,
    topic_score: TopicScoreResult | None = None,
    student_utterances: Sequence[str] = (),
) -> str:
    """Generate the student-facing diagnostic narrative.

    ``topic_score`` (2026-07-10 spec §4) is an OPT-IN keyword: when it is
    provided AND ``APOLLO_TOPIC_SCORE_SERVED`` is on, the ledger-grounded
    prompt (``topic_narrative.build_topic_narrative_prompt``) REPLACES the
    axis-based ``_SYSTEM_PROMPT``/``user_payload`` below — no other branch of
    this function changes. When the flag is off (or ``topic_score`` is
    ``None``, the default), this function's prompt assembly and output are
    unchanged from pre-topic-score behavior, including the misconception/
    negotiation recap lines appended below (those read ``rubric``/``coverage``
    directly and are unaffected by which prompt generated ``narrative``),
    modulo the final internals sanitizer, which is a no-op on clean prose."""
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    client = OpenAI()

    use_topic_prompt = topic_score is not None and topic_score_served_enabled()
    if use_topic_prompt:
        system_prompt, user_content = build_topic_narrative_prompt(
            topic_score,
            problem_text=problem_text,
            student_utterances=student_utterances,
        )
    else:
        system_prompt = _SYSTEM_PROMPT
        user_payload = {
            "problem": problem_text,
            "rubric": rubric,
            "coverage": coverage,
            "reference_required_entries": [
                {
                    "id": s["id"],
                    "type": s["entry_type"],
                    "label": s.get("content", {}).get("label"),
                    "action": s.get("content", {}).get("action"),
                }
                for s in reference_steps
            ],
        }
        user_content = json.dumps(user_payload)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.4,
        )
        narrative = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("diagnostic LLM soft-fail: %s", exc)
        narrative = "[Diagnostic narrative unavailable — the grade above is still accurate.]"

    narrative = _append_misconception_line(narrative, rubric)
    narrative = _append_negotiation_line(narrative, coverage)
    ledger_keys = (
        tuple(topic.canonical_key for topic in topic_score.topics)
        if topic_score is not None
        else ()
    )
    return sanitize_narrative(narrative, canonical_keys=ledger_keys)


def _append_negotiation_line(narrative: str, coverage: dict[str, Any]) -> str:
    """Append a one-line summary of how many KG entries the student
    negotiated.

    Class 2 Phase 3 (P3.11). The pedagogical signal here is metacognitive:
    the student looked at Apollo's understanding and acted on it. We
    surface the count so the diagnostic can credit that behavior without
    re-litigating which entries were touched.

    Source: `coverage["negotiation_counts"]` (P3.4). Tolerates legacy
    coverage dicts (no key) by treating counts as zero.

    Output is empty when nothing was negotiated. Phrased generically —
    never names individual entries."""
    counts = coverage.get("negotiation_counts") or {}
    paraphrased = int(counts.get("paraphrased", 0) or 0)
    skipped = int(counts.get("skipped", 0) or 0)
    disputed = int(counts.get("disputed", 0) or 0)
    total = paraphrased + skipped + disputed
    if total == 0:
        return narrative

    parts: list[str] = []
    if paraphrased:
        parts.append(f"{paraphrased} paraphrased")
    if skipped:
        parts.append(f"{skipped} skipped")
    if disputed:
        parts.append(f"{disputed} disputed")
    breakdown = ", ".join(parts)

    line = f"You negotiated {total} entr{'y' if total == 1 else 'ies'} with Apollo: {breakdown}."
    if narrative and not narrative.endswith("\n"):
        narrative += "\n\n"
    return narrative + line


def _append_misconception_line(narrative: str, rubric: dict[str, Any]) -> str:
    """Append a deterministic one-liner about misconceptions navigated.

    Class 2 Phase 2 (P2.9). Visible to the student only when at least
    one misconception was detected during the attempt. Phrased to avoid
    naming the misconception itself — the student already saw the
    Socratic moves; this is a recap, not a re-revelation.

    No-op when no misconceptions fired; narrative returned unchanged.
    """
    block = rubric.get("misconception_corrected") or {}
    detected = int(block.get("detected", 0) or 0)
    if detected <= 0:
        return narrative
    resolved = int(block.get("resolved", 0) or 0)
    line = (
        f"During the conversation, you and Apollo navigated through "
        f"{detected} suspected misconception"
        f"{'s' if detected != 1 else ''}; you resolved {resolved} of them."
    )
    if narrative and not narrative.endswith("\n"):
        narrative += "\n\n"
    return narrative + line
